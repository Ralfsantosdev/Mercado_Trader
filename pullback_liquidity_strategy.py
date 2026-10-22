"""
Estratégia: Pullback na EMA 50 (Institucional) + Varredura de Liquidez.

Hierarquia das Médias Móveis:
  - EMA 200: Direção macro (bancos, fundos)
  - EMA 50:  Zona de interesse institucional (onde smart money compra/vende)
  - EMA 20:  Varejo (onde os stops estão = onde varrem liquidez)

Este módulo é AUTOCONTIDO: não importa nada do resto do projeto.
Pode ser usado tanto pelo backtest quanto pelo gerador de sinais ao vivo.

Integração:
    from pullback_liquidity_strategy import (
        PullbackLiquidityConfig,
        build_signal,
        position_size_by_risk,
        risk_guard,
        Signal,
    )

Filosofia de design:
  - Todos os thresholds são RELATIVOS ao ATR (ou ao regime de volatilidade)
    — nunca fixos em dólares. Isso torna a estratégia agnóstica a qualquer
    par/timeframe.
  - O nível de liquidez (Prior_High/Prior_Low) é calculado SOMENTE sobre
    candles anteriores (shift(1) no rolling) — elimina look-ahead bias.
  - O sinal só é emitido sobre o ÚLTIMO CANDLE FECHADO (-2 em dados ao vivo).
  - Se a estrutura exige risco > max_stop_atr, o trade é descartado em vez
    de ter o stop "apertado" artificialmente.
  - Volatilidade precisa (módulo volatility.py, opcional): bloqueia entrada
    em rajada extrema (percentil > max_vol_percentile) e em lateral pura
    (Efficiency Ratio < min_efficiency_ratio), e ALARGA o stop conforme o
    regime (stop × Vol_Stop_Mult) — nunca aperta.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Literal, Dict, Any
import math
import pandas as pd

# Volatilidade precisa (Parkinson/GK/Yang-Zhang + percentil + stop adaptativo).
# Opcional: se o módulo não existir, a estratégia funciona só com ATR (fallback).
try:
    from volatility import add_volatility as _add_volatility
    _VOL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _VOL_AVAILABLE = False
    _add_volatility = None

Side = Literal["buy", "sell"]


@dataclass
class PullbackLiquidityConfig:
    # ── Médias móveis ──────────────────────────────────────────────────────
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200        # EMA 200 — direção macro (institucional)
    atr_period: int = 14
    adx_min: float = 25.0       # ADX mínimo para confirmar tendência forte

    # ── Distâncias relativas ao ATR ────────────────────────────────────────
    pullback_tolerance_atr: float = 0.15   # Quão perto a EMA50 precisa estar do Low/High
    liquidity_lookback: int = 20           # Janela para Prior_High/Prior_Low
    sweep_buffer_atr: float = 0.02         # Margem além do nível para contar como sweep

    # ── Qualidade do candle de gatilho ─────────────────────────────────────
    min_body_fraction: float = 0.40        # Corpo >= 40% do range (mais exigente)
    min_close_location_buy: float = 0.65   # Fechamento no terço superior (compra)
    max_close_location_sell: float = 0.35  # Fechamento no terço inferior (venda)

    # ── Stop por estrutura ─────────────────────────────────────────────────
    stop_buffer_atr: float = 0.10          # Buffer além do extremo da varredura
    min_stop_atr: float = 0.80             # Piso de volatilidade (ruído normal)
    max_stop_atr: float = 1.50             # Teto: estrutura muito distante → descarta

    # ── Volatilidade precisa (regime + stop adaptativo) ────────────────────
    vol_window: int = 20                   # Janela dos estimadores OHLC
    vol_percentile_lookback: int = 200     # História p/ o percentil (relativo ao ativo)
    vol_percentile_min: int = 60           # Mínimo de dados p/ existir percentil
    max_vol_percentile: float = 90.0       # Acima de P90 (rajada/news) → não entra
    min_efficiency_ratio: float = 0.15     # ER < 0.15 = lateral pura → não entra
    adaptive_stop: bool = True             # Escala stop com o regime de volatilidade
    stop_vol_gain: float = 0.6             # Até 1.6× o stop quando o percentil = 100

    rr_target: float = 3.0                 # R:R mínimo do alvo (3R — mais exigente)
    min_rr_to_liquidity: float = 1.50      # Liquidez contrária não pode estar perto

    # ── Gestão de risco ────────────────────────────────────────────────────
    risk_per_trade_pct: float = 0.25       # % do capital arriscado por trade
    daily_loss_limit_pct: float = 1.00     # Limite de perda diária acumulada
    max_consecutive_losses: int = 2        # Pausa após 2 losses seguidos
    cooldown_bars: int = 3                 # Candles de cooldown após loss


@dataclass
class Signal:
    side: Side
    entry: float
    stop: float
    take: float
    risk_price: float
    rr: float
    atr: float
    adx: float
    liquidity_level: float
    sweep_extreme: float
    reason: str
    vol_percentile: Optional[float] = None   # onde a vol está na própria história
    vol_regime: str = ""                     # BAIXA / NORMAL / ALTA / EXTREMA
    stop_mult: float = 1.0                   # multiplicador adaptativo aplicado

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────────────────

def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def add_core_indicators(df: pd.DataFrame, cfg: PullbackLiquidityConfig) -> pd.DataFrame:
    """
    Adiciona EMA20, EMA50, EMA200, ATR e Prior_High/Prior_Low ao DataFrame.

    Preserva colunas já existentes (calculadas por indicators.py).
    ADX não é recalculado aqui — espera-se que venha do indicators.py.

    IMPORTANTE: Prior_High/Low usam shift(1) no rolling para garantir que
    o extremo do lookback nunca inclui o candle atual (sem look-ahead bias).
    """
    out = df.copy()

    if "EMA_20" not in out.columns:
        out["EMA_20"] = out["Close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    if "EMA_50" not in out.columns:
        out["EMA_50"] = out["Close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    if "EMA_200" not in out.columns:
        out["EMA_200"] = out["Close"].ewm(span=cfg.ema_trend, adjust=False).mean()
    if "ATR" not in out.columns:
        out["ATR"] = _true_range(out).ewm(alpha=1 / cfg.atr_period, adjust=False).mean()

    # Volatilidade precisa (percentil de regime, multiplicador de stop, ER)
    if _VOL_AVAILABLE and "Vol_Percentile" not in out.columns:
        out = _add_volatility(
            out,
            window=cfg.vol_window,
            percentile_lookback=cfg.vol_percentile_lookback,
            percentile_min_periods=cfg.vol_percentile_min,
            stop_gain=cfg.stop_vol_gain,
            extreme_percentile=cfg.max_vol_percentile,
        )

    out["Prior_High"] = (
        out["High"]
        .rolling(cfg.liquidity_lookback, min_periods=cfg.liquidity_lookback)
        .max()
        .shift(1)
    )
    out["Prior_Low"] = (
        out["Low"]
        .rolling(cfg.liquidity_lookback, min_periods=cfg.liquidity_lookback)
        .min()
        .shift(1)
    )

    return out


def _candle_quality(row: pd.Series, side: Side, cfg: PullbackLiquidityConfig) -> bool:
    """Verifica se o candle de gatilho tem corpo e localização de fechamento adequados."""
    rng = float(row["High"] - row["Low"])
    if rng <= 0:
        return False

    body = abs(float(row["Close"] - row["Open"]))
    body_fraction = body / rng
    close_location = float((row["Close"] - row["Low"]) / rng)

    if body_fraction < cfg.min_body_fraction:
        return False

    if side == "buy":
        return row["Close"] > row["Open"] and close_location >= cfg.min_close_location_buy

    return row["Close"] < row["Open"] and close_location <= cfg.max_close_location_sell


def _trend_ok(row: pd.Series, prev: pd.Series, side: Side, cfg: PullbackLiquidityConfig) -> bool:
    """
    Filtro de tendência institucional:
    1. ADX >= mínimo (tendência forte)
    2. EMA 200 confirma direção macro
    3. EMA 50 > EMA 200 (compra) ou EMA 50 < EMA 200 (venda)
    4. EMA 20 na mesma direção (inércia)
    """
    if "ADX" not in row.index or pd.isna(row["ADX"]) or float(row["ADX"]) < cfg.adx_min:
        return False

    ema200 = float(row["EMA_200"]) if "EMA_200" in row.index and not pd.isna(row["EMA_200"]) else None
    ema50 = float(row["EMA_50"])
    ema20 = float(row["EMA_20"])

    if side == "buy":
        # Direção macro: preço acima da EMA200
        if ema200 is not None and float(row["Close"]) < ema200:
            return False
        # EMA50 acima da EMA200 (smart money comprado)
        if ema200 is not None and ema50 < ema200:
            return False
        # EMA20 subindo (inércia)
        return ema20 > ema50 and ema20 > prev["EMA_20"]

    # sell
    # Direção macro: preço abaixo da EMA200
    if ema200 is not None and float(row["Close"]) > ema200:
        return False
    # EMA50 abaixo da EMA200 (smart money vendido)
    if ema200 is not None and ema50 > ema200:
        return False
    # EMA20 descendo (inércia)
    return ema20 < ema50 and ema20 < prev["EMA_20"]


def _pullback_touched(row: pd.Series, side: Side, cfg: PullbackLiquidityConfig) -> bool:
    """
    Preço corrigiu até a zona institucional (EMA 50).
    Smart Money compra/vende nessas zonas — o varejo entra na EMA 20.
    """
    atr = float(row["ATR"])
    tol = atr * cfg.pullback_tolerance_atr
    ema50 = float(row["EMA_50"])

    if side == "buy":
        return float(row["Low"]) <= ema50 + tol

    return float(row["High"]) >= ema50 - tol


def _sweep(row: pd.Series, side: Side, cfg: PullbackLiquidityConfig) -> bool:
    """
    Varredura de liquidez: candle ultra passou o nível mas FECHOU de volta.
      BUY:  Low < Prior_Low - buffer  AND  Close > Prior_Low
      SELL: High > Prior_High + buffer AND  Close < Prior_High
    """
    atr = float(row["ATR"])
    buffer_ = atr * cfg.sweep_buffer_atr

    if side == "buy":
        level = row["Prior_Low"]
        if pd.isna(level):
            return False
        return float(row["Low"]) < level - buffer_ and float(row["Close"]) > level

    level = row["Prior_High"]
    if pd.isna(level):
        return False
    return float(row["High"]) > level + buffer_ and float(row["Close"]) < level


def _next_opposing_liquidity(
    df: pd.DataFrame, idx: int, side: Side, lookback: int = 80
) -> Optional[float]:
    """
    Proxy conservador para liquidez contrária usando extremos históricos.

    BUY  → menor High anterior acima da entrada (resistência).
    SELL → maior Low  anterior abaixo da entrada (suporte).

    Pode ser substituído pelo detector de Equal_High/Equal_Low do indicators.py.
    """
    start = max(0, idx - lookback)
    hist = df.iloc[start:idx]
    if hist.empty:
        return None

    entry = float(df.iloc[idx]["Close"])
    if side == "buy":
        candidates = hist.loc[hist["High"] > entry, "High"]
        return float(candidates.min()) if not candidates.empty else None

    candidates = hist.loc[hist["Low"] < entry, "Low"]
    return float(candidates.max()) if not candidates.empty else None


# ─────────────────────────────────────────────────────────────────────────────
# API pública principal
# ─────────────────────────────────────────────────────────────────────────────

def build_signal(
    df: pd.DataFrame,
    cfg: Optional[PullbackLiquidityConfig] = None,
    use_closed_candle: bool = True,
) -> Optional[Signal]:
    """
    Gera um sinal apenas quando TODA a sequência acontece no candle fechado:
      tendência (EMA200 > EMA50 + ADX > 25) → pullback até EMA50 →
      sweep de liquidez (varre varejo na EMA20) → candle de gatilho forte.

    Parâmetros
    ----------
    df : DataFrame com OHLC + ADX (calculado pelo indicators.py).
    cfg : configuração; usa defaults se None.
    use_closed_candle : True (padrão) usa -2 (candle fechado); False usa -1
                        (candle em formação — apenas para backtest offline).

    Retorna None se nenhuma condição for satisfeita.
    """
    cfg = cfg or PullbackLiquidityConfig()
    data = add_core_indicators(df, cfg)

    min_bars = max(cfg.ema_slow, cfg.liquidity_lookback) + 5
    if len(data) < min_bars:
        return None

    idx = len(data) - 2 if use_closed_candle else len(data) - 1
    if idx < 2:
        return None

    row = data.iloc[idx]
    prev = data.iloc[idx - 1]

    required = [
        "Open", "High", "Low", "Close",
        "EMA_20", "EMA_50", "ATR", "ADX",
        "Prior_High", "Prior_Low",
    ]
    if any(c not in data.columns for c in required):
        return None
    if row[required].isna().any():
        return None

    atr = float(row["ATR"])
    if atr <= 0:
        return None

    # ── Filtro de Estado do Mercado ──────────────────────────────────────
    # Só opera em TRANSIÇÃO ou DESEQUILIBRIO (não fica no equilíbrio)
    market_zone = row.get("Market_Zone", "EQUILIBRIO")
    if market_zone == "EQUILIBRIO":
        return None  # Não opera em mercado lateral

    # ── Filtro de volatilidade precisa (regime + chop) ───────────────────
    # Percentil rolante = relativo ao próprio ativo/timeframe (serve para
    # ouro, forex e índices sem calibrar nada). ER = Efficiency Ratio de
    # Kaufman (0 = lateral pura, 1 = tendência reta).
    def _num(value) -> Optional[float]:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None

    vol_pct = _num(row.get("Vol_Percentile"))
    efficiency = _num(row.get("Efficiency_Ratio"))
    vol_regime = str(row.get("Vol_Regime") or "")
    vol_mult = 1.0
    if cfg.adaptive_stop:
        vm = _num(row.get("Vol_Stop_Mult"))
        if vm is not None and vm > 0:
            vol_mult = vm

    if vol_pct is not None and vol_pct > cfg.max_vol_percentile:
        return None  # rajada extrema (notícia/expansão) → whipsaw, stop morre primeiro
    if efficiency is not None and efficiency < cfg.min_efficiency_ratio:
        return None  # lateral pura: sem continuidade, varre os dois lados

    for side in ("buy", "sell"):
        side: Side
        if not _trend_ok(row, prev, side, cfg):
            continue
        if not _pullback_touched(row, side, cfg):
            continue
        if not _sweep(row, side, cfg):
            continue
        if not _candle_quality(row, side, cfg):
            continue

        entry = float(row["Close"])

        # Buffer e piso do stop escalam com o regime de volatilidade:
        # em rajada (percentil alto) o ruído por candle é maior que a média
        # que o ATR(14) ainda refletiu — stop fixo morre aqui.
        if side == "buy":
            liquidity_level = float(row["Prior_Low"])
            sweep_extreme = float(row["Low"])
            structure_stop = sweep_extreme - cfg.stop_buffer_atr * atr * vol_mult
            structure_distance = entry - structure_stop
        else:
            liquidity_level = float(row["Prior_High"])
            sweep_extreme = float(row["High"])
            structure_stop = sweep_extreme + cfg.stop_buffer_atr * atr * vol_mult
            structure_distance = structure_stop - entry

        # Estrutura exige risco demais → ignora o trade.
        if structure_distance > cfg.max_stop_atr * atr * vol_mult:
            continue

        # Espaço mínimo para o ruído REAL do momento (não a média histórica).
        risk_price = max(structure_distance, cfg.min_stop_atr * atr * vol_mult)
        stop = entry - risk_price if side == "buy" else entry + risk_price
        take = (
            entry + cfg.rr_target * risk_price
            if side == "buy"
            else entry - cfg.rr_target * risk_price
        )

        # Liquidez contrária muito perto → sem espaço estatístico.
        opposing = _next_opposing_liquidity(data, idx, side)
        if opposing is not None:
            room = (opposing - entry) if side == "buy" else (entry - opposing)
            if room / risk_price < cfg.min_rr_to_liquidity:
                continue

        vol_note = ""
        if vol_pct is not None:
            vol_note = f" · vol P{vol_pct:.0f}{(' ' + vol_regime) if vol_regime else ''}"
        if vol_mult != 1.0:
            vol_note += f" · stop×{vol_mult:.2f}"

        return Signal(
            side=side,
            entry=entry,
            stop=float(stop),
            take=float(take),
            risk_price=float(risk_price),
            rr=float(cfg.rr_target),
            atr=atr,
            adx=float(row["ADX"]),
            liquidity_level=liquidity_level,
            sweep_extreme=sweep_extreme,
            reason=f"trend(EMA200+EMA50+ADX>{cfg.adx_min})+pullback(EMA50)+sweep+trigger{vol_note}",
            vol_percentile=vol_pct,
            vol_regime=vol_regime,
            stop_mult=float(vol_mult),
        )

    return None


def position_size_by_risk(
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    tick_size: float,
    tick_value: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    """
    Dimensiona volume (lotes) pelo risco financeiro real do símbolo.

    Usa tick_size e tick_value obtidos de symbol_info() do MT5 — assim o
    cálculo é correto para XAUUSD, índices e qualquer outro instrumento,
    sem depender de CONTRACT_SIZE fixo.

    Retorna 0.0 se nem o lote mínimo cabe dentro do risco configurado.
    """
    risk_cash = equity * (risk_pct / 100.0)
    distance = abs(entry - stop)
    if risk_cash <= 0 or distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0

    cash_per_lot_at_stop = (distance / tick_size) * tick_value
    if cash_per_lot_at_stop <= 0:
        return 0.0

    raw = risk_cash / cash_per_lot_at_stop
    stepped = math.floor(raw / volume_step) * volume_step
    volume = max(volume_min, min(volume_max, stepped))

    # Verifica se o lote mínimo já excede o risco → NÃO opera.
    min_risk_cash = (distance / tick_size) * tick_value * volume_min
    if min_risk_cash > risk_cash:
        return 0.0

    return round(volume, 8)


def risk_guard(
    current_daily_pnl_pct: float,
    consecutive_losses: int,
    has_open_position: bool,
    cfg: Optional[PullbackLiquidityConfig] = None,
) -> tuple[bool, str]:
    """
    Guarda de risco que bloqueia novas entradas quando:
      - Já existe posição aberta (sem pirâmide).
      - Perda diária acumulada atingiu o limite.
      - Sequência de losses consecutivos atingiu o limite (pausa obrigatória).

    Retorna (permitido: bool, motivo: str).
    """
    cfg = cfg or PullbackLiquidityConfig()

    if has_open_position:
        return False, "position_already_open"
    if current_daily_pnl_pct <= -abs(cfg.daily_loss_limit_pct):
        return False, "daily_loss_limit"
    if consecutive_losses >= cfg.max_consecutive_losses:
        return False, "consecutive_loss_limit"

    return True, "ok"
