"""
Volatilidade PRECISA — estimadores OHLC + regime + multiplicador de stop.

Por que o ATR sozinho não basta:
o ATR(14) é uma média móvel exponencial do True Range. Reage devagar a
mudança de regime e ignora a informação intracandle (abertura vs fechamento).
Sinais com stop fixo em múltiplo de ATR morrem em rajada de volatilidade e em
lateral — é exatamente o "stop atrás do stop".

Aqui combinamos 4 estimadores consagrados (todos calculados só com dados que
existiam na época, janela rolante, sem look-ahead):

  · Parkinson     — só High/Low: eficiência ~2x maior que close-to-close
  · Garman-Klass  — OHLC: usa abertura e fechamento, filtra o gap de abertura
  · Yang-Zhang    — OHLC + overnight: o mais preciso para dados intradiários
  · Realized      — desvio-padrão dos retornos logarítmicos (close-to-close)

Em cima do composto (mediana dos 4 estimadores, mesma unidade: σ por candle):

  · Vol_Precise / Vol_Precise_Pct — volatilidade por candle (fração / %)
  · Vol_Percentile — onde a vol atual está na própria história (0-100)
  · Vol_Regime     — BAIXA / NORMAL / ALTA / EXTREMA (percentil)
  · Vol_Stop_Mult  — multiplicador do stop: só ALARGA quando a vol está acima
                     da própria mediana; nunca aperta o stop
  · Efficiency_Ratio — ER de Kaufman: 0 = lateral puro (chop), 1 = tendência

Módulo autocontido: apenas pandas + numpy (não importa o resto do projeto).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

_LN2 = math.log(2.0)
_FOUR_LN2 = 4.0 * _LN2                 # constante de Parkinson
_TWO_LN2_M1 = 2.0 * _LN2 - 1.0         # constante de Garman-Klass (≈0.3863)

REGIME_BAIXA = "BAIXA"
REGIME_NORMAL = "NORMAL"
REGIME_ALTA = "ALTA"
REGIME_EXTREMA = "EXTREMA"

_DEFAULT_STOP_GAIN = 0.6    # stop até 1.6× quando o percentil chega a 100
_BASELINE_PCT = 50.0        # abaixo da mediana da própria história, mult = 1.0


def _min_periods(window: int) -> int:
    return max(2, window // 2)


def parkinson_vol(high: pd.Series, low: pd.Series, window: int = 20) -> pd.Series:
    """Parkinson (1980): só High/Low. σ por candle (fração do preço)."""
    hl = np.log(high / low)
    var = (hl ** 2).rolling(window, min_periods=_min_periods(window)).mean() / _FOUR_LN2
    return np.sqrt(var.clip(lower=0.0))


def garman_klass_vol(open_: pd.Series, high: pd.Series, low: pd.Series,
                     close: pd.Series, window: int = 20) -> pd.Series:
    """Garman-Klass (1980): OHLC. σ por candle (fração do preço)."""
    hl2 = np.log(high / low) ** 2
    co2 = np.log(close / open_) ** 2
    var = (0.5 * hl2 - _TWO_LN2_M1 * co2).rolling(
        window, min_periods=_min_periods(window)
    ).mean()
    return np.sqrt(var.clip(lower=0.0))


def yang_zhang_vol(open_: pd.Series, high: pd.Series, low: pd.Series,
                   close: pd.Series, window: int = 20) -> pd.Series:
    """
    Yang-Zhang (2000): overnight + abertura-fechamento + Rogers-Satchell.
    O estimador mais eficiente em dados intradiários (usa o gap de abertura).
    σ por candle (fração do preço).
    """
    if window < 3:
        raise ValueError("Yang-Zhang exige window >= 3")

    prev_close = close.shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        overnight = np.log(open_ / prev_close)
        openclose = np.log(close / open_)
        rs = (np.log(high / close) * np.log(high / open_) +
              np.log(low / close) * np.log(low / open_))

    mp = _min_periods(window)
    var_o = overnight.rolling(window, min_periods=mp).var(ddof=1)
    var_c = openclose.rolling(window, min_periods=mp).var(ddof=1)
    var_rs = rs.rolling(window, min_periods=mp).sum() / (window - 1)

    k = 0.34 / (1.34 + (window + 1.0) / (window - 1.0))
    var = var_o.fillna(0.0) + k * var_c.fillna(0.0) + (1.0 - k) * var_rs.fillna(0.0)
    # antes do mínimo de dados não há estimativa válida
    valid = overnight.notna() & rs.notna()
    var = var.where(valid)
    return np.sqrt(var.clip(lower=0.0))


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    """Realized volatility: σ dos retornos logarítmicos (close-to-close)."""
    rets = np.log(close / close.shift(1))
    return rets.rolling(window, min_periods=_min_periods(window)).std(ddof=1)


def efficiency_ratio(close: pd.Series, window: int = 10) -> pd.Series:
    """
    Efficiency Ratio de Kaufman: direção líquida / caminho total.
    0 = lateral puro (chop — o cenário que mata stop), 1 = tendência reta.
    """
    net = (close - close.shift(window)).abs()
    path = close.diff().abs().rolling(window, min_periods=window).sum()
    return (net / path.replace(0.0, np.nan)).clip(0.0, 1.0)


def rolling_percentile(series: pd.Series, lookback: int = 200,
                       min_periods: int = 60) -> pd.Series:
    """
    Percentil rolante (0-100) da série na própria história — relativo ao
    ativo/timeframe, por isso serve para qualquer instrumento sem calibrar.
    """
    if series.notna().sum() < max(2, min_periods // 2):
        return pd.Series(np.nan, index=series.index)
    filled = series.ffill().bfill()
    try:
        pct = filled.rolling(lookback, min_periods=min_periods).rank(pct=True) * 100.0
    except AttributeError:  # pandas < 1.4
        pct = filled.rolling(lookback, min_periods=min_periods).apply(
            lambda w: float(pd.Series(w).rank(pct=True).iloc[-1]) * 100.0, raw=True
        )
    return pct.where(series.notna())


def regime_label(percentile: pd.Series, extreme: float = 90.0, high: float = 75.0,
                 low: float = 25.0) -> pd.Series:
    """Rótulo do regime de volatilidade a partir do percentil."""
    p = percentile
    labels = np.select(
        [p.isna(), p >= extreme, p >= high, p < low],
        ["", REGIME_EXTREMA, REGIME_ALTA, REGIME_BAIXA],
        default=REGIME_NORMAL,
    )
    return pd.Series(labels, index=p.index, name="Vol_Regime")


def stop_multiplier(percentile: pd.Series, gain: float = _DEFAULT_STOP_GAIN,
                    baseline_pct: float = _BASELINE_PCT) -> pd.Series:
    """
    Multiplicador adaptativo do stop em função do percentil de volatilidade.

    · percentil <= 50 → 1.0 (a vol já está na média histórica ou abaixo: nada a fazer)
    · percentil 100   → 1.0 + gain (padrão 1.6× — a rajada atual é maior que
                        a média que o ATR(14) ainda não pegou)
    Nunca aperta o stop abaixo de 1.0.
    """
    span = 100.0 - baseline_pct
    above = ((percentile - baseline_pct) / span).clip(lower=0.0, upper=1.0)
    mult = 1.0 + above * float(gain)
    return mult.clip(lower=1.0, upper=1.0 + float(gain))


def add_volatility(df: pd.DataFrame, window: int = 20, er_window: int = 10,
                   percentile_lookback: int = 200, percentile_min_periods: int = 60,
                   stop_gain: float = _DEFAULT_STOP_GAIN,
                   extreme_percentile: float = 90.0,
                   high_percentile: float = 75.0,
                   low_percentile: float = 25.0) -> pd.DataFrame:
    """
    Adiciona ao DataFrame as colunas de volatilidade precisa:

        Vol_Parkinson, Vol_GK, Vol_YZ, Vol_Realized   (σ por candle, fração)
        Vol_Precise (mediana dos 4) · Vol_Precise_Pct (% por candle)
        Vol_Percentile (0-100) · Vol_Regime · Vol_Stop_Mult
        Efficiency_Ratio (0-1, chop vs tendência)
    """
    out = df.copy()
    if not {"Open", "High", "Low", "Close"}.issubset(out.columns):
        return out

    o, h, l, c = out["Open"], out["High"], out["Low"], out["Close"]

    estimators = pd.concat(
        [
            parkinson_vol(h, l, window),
            garman_klass_vol(o, h, l, c, window),
            yang_zhang_vol(o, h, l, c, window),
            realized_vol(c, window),
        ],
        axis=1,
    )
    out["Vol_Parkinson"], out["Vol_GK"], out["Vol_YZ"], out["Vol_Realized"] = (
        estimators.iloc[:, 0], estimators.iloc[:, 1], estimators.iloc[:, 2], estimators.iloc[:, 3]
    )
    out["Vol_Precise"] = estimators.median(axis=1, skipna=True)
    out["Vol_Precise_Pct"] = out["Vol_Precise"] * 100.0

    out["Vol_Percentile"] = rolling_percentile(
        out["Vol_Precise"], lookback=percentile_lookback,
        min_periods=percentile_min_periods,
    )
    out["Vol_Regime"] = regime_label(
        out["Vol_Percentile"], extreme=extreme_percentile,
        high=high_percentile, low=low_percentile,
    )
    out["Vol_Stop_Mult"] = stop_multiplier(out["Vol_Percentile"], gain=stop_gain)
    out["Efficiency_Ratio"] = efficiency_ratio(c, er_window)
    return out


def vol_summary(df: Optional[pd.DataFrame]) -> dict:
    """Resumo do último candle para exibição (painel/gráfico/alerta)."""
    empty = {
        "vol_pct": None, "percentile": None, "regime": "",
        "stop_mult": 1.0, "efficiency": None, "blocked": False, "reason": "",
    }
    if df is None or df.empty or "Vol_Percentile" not in df.columns:
        return empty
    row = df.iloc[-1]

    def _f(col) -> Optional[float]:
        v = row.get(col)
        return float(v) if v is not None and pd.notna(v) else None

    vol_pct = _f("Vol_Precise_Pct")
    pct = _f("Vol_Percentile")
    er = _f("Efficiency_Ratio")
    mult = _f("Vol_Stop_Mult") or 1.0
    regime = str(row.get("Vol_Regime") or "")

    reasons = []
    if pct is not None and regime == REGIME_EXTREMA:
        reasons.append(f"volatilidade extrema (P{pct:.0f})")
    if er is not None and er < 0.15:
        reasons.append(f"lateral/chop (ER {er:.2f})")

    return {
        "vol_pct": vol_pct,
        "percentile": pct,
        "regime": regime,
        "stop_mult": mult,
        "efficiency": er,
        "blocked": bool(reasons),
        "reason": " · ".join(reasons),
    }
