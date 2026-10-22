"""
Sinais baseados em regras (estratégia MM9/MM21/MM200 + RSI) e modelo de ML
com validação temporal correta (sem vazamento / look-ahead bias).

Correções aplicadas aqui:
- Sinal não morre mais por causa do Volume zerado do Yahoo para forex:
  quando o volume é inútil (>= 90% dos valores == 0), a confirmação passa a
  ser feita pela aceleração do ATR (volatilidade), que existe de verdade.
- Thresholds de RSI configuráveis (não mais fixos em 10/90, que quase nunca
  disparam).
- Gatilho opcional por CRUZAMENTO real MM9xMM21 (não só "nível").
- "Confiança" do ML deixou de ser a probabilidade da classe vencedora (que é
  sempre >= 50% e ilude) — agora é a MARGEM |p - 0.5|, com leitura honesta.
"""

import pandas as pd
import numpy as np
from typing import Optional
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score

# Estratégia quantitativa de pullback + liquidez (módulo autocontido)
try:
    from pullback_liquidity_strategy import (
        PullbackLiquidityConfig,
        build_signal as _pls_build_signal,
    )
    _PLS_AVAILABLE = True
except ImportError:
    _PLS_AVAILABLE = False

FEATURE_COLUMNS = [
    "SMA_9", "SMA_21", "SMA_200", "RSI", "MACD", "MACD_Hist", "ATR", "Volatility", "BB_Position",
    "ADX", "Ret_1", "Ret_3", "Ret_5", "Sma_9_vs_21", "Rsi_vs_50",
]

# Sessões de alta liquidez do mercado forex (horário UTC).
# Operar fora delas = spread maior e movimentos sem continuidade.
# Valores são (hora_inicio, hora_fim) em UTC; hora_fim exclusiva.
SESSION_UTC = {
    "Sessão de Tóquio": [(0, 9)],
    "Sessão de Londres": [(7, 16)],
    "Sessão de Nova York": [(12, 21)],
    "Londres + Nova York (melhor janela)": [(12, 16)],
    "Tóquio + Londres": [(7, 9)],
    "Cripto 24h": [(0, 24)],
}


# ── Índice do candle acionável ────────────────────────────────────────────────
# Fonte ÚNICA de verdade. O motor pullback_liquidity avalia o candle FECHADO e
# grava o sinal em `index[-2]`; os demais gatilhos gravam no candle em formação
# (`index[-1]`). Quem lê `df["Signal"]` sem consultar isto pega a linha errada e
# o robô vira no-op (o sinal existe, mas ninguém enxerga).
SIGNAL_IDX_ATTR = "signal_idx"


def signal_index(df: pd.DataFrame, trigger_mode: str = "") -> int:
    """
    Índice relativo (negativo) do candle que carrega o sinal acionável.

    Prioriza `df.attrs["signal_idx"]` (gravado por generate_rule_based_signals).
    Fallback por trigger_mode para quando attrs vier de um cache antigo.
    """
    idx = df.attrs.get(SIGNAL_IDX_ATTR)
    if isinstance(idx, (int, np.integer)) and -len(df) <= int(idx) < 0:
        return int(idx)
    return -2 if trigger_mode == "pullback_liquidity" else -1


def _is_in_session(index: pd.DatetimeIndex, session: str,
                   tz_offset_hours: float = 0.0) -> pd.Series:
    """
    Retorna máscara booleana: quais candles estão dentro da sessão escolhida.

    `tz_offset_hours` = diferença (servidor − UTC). Candles do MT5 vêm no
    horário do SERVIDOR da corretora (ex.: UTC+2/+3), então sem esta correção a
    janela "Londres + Nova York (12–16 UTC)" estaria deslocada 2–3 h.
    """
    if session not in SESSION_UTC:
        return pd.Series(True, index=index)
    hours = pd.Series((np.asarray(index.hour, dtype=float) - float(tz_offset_hours or 0.0)) % 24,
                      index=index)
    windows = SESSION_UTC[session]
    mask = pd.Series(False, index=index)
    for start, end in windows:
        if end == 24:
            mask |= hours >= start
        elif start < end:
            mask |= (hours >= start) & (hours < end)
        else:  # janela que cruza a meia-noite
            mask |= (hours >= start) | (hours < end)
    return mask


def volume_is_meaningful(df: pd.DataFrame, zero_ratio_threshold: float = 0.9) -> bool:
    """Yahoo devolve Volume=0 para forex intradiário. Detecta e ignora."""
    vol = df["Volume"].fillna(0)
    if vol.eq(0).all():
        return False
    return (vol > 0).mean() >= (1 - zero_ratio_threshold)


def _confirmation_column(df: pd.DataFrame, use_volume: bool) -> pd.Series:
    """Confirmação de força do movimento: volume real quando existe, senão ATR em alta."""
    if use_volume:
        return df["Volume"] > df["Volume_MA"]
    atr_ma = df["ATR"].rolling(20).mean()
    return df["ATR"] > atr_ma


def apply_live_confirmation(df: pd.DataFrame, quote: Optional[dict] = None,
                            max_spread_pips: float = 0.0, min_tick_volume: float = 0.0,
                            pip_size: Optional[float] = None,
                            tz_offset_hours: Optional[float] = None) -> pd.DataFrame:
    """
    Filtro de confirmação AO VIVO usando o que o terminal MT5 oferece de verdade:

    - spread real (ask - bid): bloqueia a entrada quando o custo da operação
      está alto demais (ex.: em horário de notícia).
    - volume real do último tick e do dia: bloqueia quando o símbolo não tem
      volume/liquidez real no momento (sinal "morto" = spread inflado).

    Só avalia o ÚLTIMO candle (o que executaria agora). Se o filtro bloquear,
    zera o sinal nesse candle e guarda o motivo em df.attrs["live_block_msg"].
    Ambos os limites = 0 desligam o filtro (comportamento original).
    """
    df = df.copy()
    _ai = int(df.attrs.get(SIGNAL_IDX_ATTR, -1) or -1)

    def _apply(signal, live):
        if signal is None or not live:
            return signal
        reasons = []
        spread = float(live.get("spread") or 0.0)
        # converte spread pra pips usando o pip_size informado; se não vier,
        # infere do próprio spread (digits/point fazem isso complicado) — usamos
        # o tamanho de pip padrão do par quando pip_size > 0
        spread_pips = 0.0
        if spread > 0:
            _ps = pip_size if pip_size else (1.0 / (10 ** (float(live.get("digits") or 5))))
            spread_pips = spread / _ps if _ps else 0.0
            if max_spread_pips > 0 and spread_pips > max_spread_pips:
                reasons.append(f"spread {spread_pips:.2f}p > {max_spread_pips:.2f}p")

        # Frescor do tick = prova REAL de mercado aberto. O volume reportado
        # NÃO serve pra isso: ouro/CFD costumam vir com tick_volume=0 e
        # day_volume=0 mesmo com o mercado liquido — o filtro antigo bloqueava
        # toda entrada em XAU-USD o tempo todo.
        _tick_epoch = int(live.get("time") or 0)
        age_s = None
        if _tick_epoch > 0:
            import time as _time_mod
            age_s = _time_mod.time() - (_tick_epoch - float(tz_offset_hours or 0.0) * 3600.0)
        if age_s is not None and age_s > 900:
            reasons.append(f"ultimo tick ha {age_s / 60.0:.0f} min (mercado fechado)")

        tick_vol = float(live.get("tick_volume") or 0.0)
        if min_tick_volume > 0 and tick_vol > 0 and tick_vol < min_tick_volume:
            reasons.append(f"volume tick {tick_vol:.0f} < {min_tick_volume:.0f}")

        if reasons:
            df.attrs["signal_block_msg"] = " · ".join(reasons)
            return 0
        df.attrs["signal_block_msg"] = ""
        return signal

    if "Signal" in df.columns and len(df) > 0:
        _ai = _ai if -len(df) <= _ai < 0 else -1
        df.loc[df.index[_ai], "Signal"] = _apply(int(df.loc[df.index[_ai], "Signal"]), quote)
    return df


def apply_live_candle(df: pd.DataFrame, quote: Optional[dict] = None) -> pd.DataFrame:
    """
    Atualiza o ÚLTIMO candle com o preço ao vivo do MT5 (bid/ask) para o gráfico
    "respirar" entre refreshes. Não recalcula indicadores nem sinais — só ajusta
    Close/High/Low do candle em formação com o tick atual.
    """
    if df is None or df.empty or not quote or not quote.get("bid"):
        return df
    df = df.copy()
    bid = float(quote["bid"])
    ask = float(quote["ask"]) or bid
    mid = (bid + ask) / 2
    i = df.index[-1]
    df.loc[i, "Close"] = mid
    df.loc[i, "High"] = max(float(df.loc[i, "High"]), mid)
    df.loc[i, "Low"] = min(float(df.loc[i, "Low"]), mid)
    return df


def generate_rule_based_signals(
    df: pd.DataFrame,
    rsi_overbought: int = 70,
    rsi_oversold: int = 30,
    use_volume: bool = True,
    use_cross: bool = True,
    trigger_mode: str = "auto",
    pullback_ema: int = 50,
    session: str = "",
    min_adx: int = 20,
    live: dict = None,
    max_spread_pips: float = 3.0,
    min_tick_volume: int = 5,
    pip_size: float = 0.0001,
    fimathe_hour: int = 20,
    fimathe_candles: int = 4,
    fimathe_max_pips: float = 30.0,
    pls_config_kwargs: dict = None,
    max_vol_percentile: float = 90.0,
    min_efficiency_ratio: float = 0.15,
    tz_offset_hours: float = 0.0,
) -> pd.DataFrame:
    """
    Estratégia:
    - MM200 define o viés (só compra acima, só venda abaixo)
    - Gatilho configurável via `trigger_mode`:
        "slope"    -> inclinação do MM9 (preserva o comportamento antigo)
        "cross"    -> cruzamento real MM9 <-> MM21
        "pullback" -> retorno do preço à EMA (`pullback_ema`, padrão 50):
                      só compra quando abertura/perto toca a EMA no viés de alta
                      e fecha acima dela (espera zona de retângulo + confirmação)
    - RSI extremo confirma (limites configuráveis)
    - Volume confirma a força; se volume não existir, usa ATR em alta
    - Filtro de sessão: só opera em horários de alta liquidez (opcional)
    - Filtro de ADX: só opera quando há tendência forte (opcional)
    - Filtro de volatilidade precisa: bloqueia sinal acima de
      `max_vol_percentile` (rajada extrema = whipsaw) e com
      `min_efficiency_ratio` abaixo do mínimo (lateral pura / chop).
      Os dois limiares são RELATIVOS (percentil e ER), então valem para
      qualquer ativo/timeframe sem calibração.
    """
    df = df.copy()
    df["Signal"] = 0
    # Padrão: sinal no candle em formação. O branch pullback_liquidity ajusta
    # para -2 quando o motor quantitativo estiver disponível.
    df.attrs[SIGNAL_IDX_ATTR] = -1

    # normaliza index p/ UTC (yfinance entrega tz-aware; AwesomeAPI já é UTC)
    index_utc = df.index
    if isinstance(index_utc, pd.DatetimeIndex) and getattr(index_utc, "tz", None) is not None:
        index_utc = index_utc.tz_convert("UTC")
    session_ok = _is_in_session(index_utc, session, tz_offset_hours=tz_offset_hours)

    has_volume = volume_is_meaningful(df)
    effective_use_volume = bool(use_volume and has_volume)
    df.attrs["use_volume"] = effective_use_volume

    trend_up = df["Close"] > df["SMA_200"]
    trend_down = df["Close"] < df["SMA_200"]

    if trigger_mode == "auto":
        # mapeia o comportamento antigo: use_cross = slope, senão nível MM9
        trigger_mode = "slope" if use_cross else "level"

    if trigger_mode == "slope":
        # Direção do próprio MM9 (inclinação): compra quando o MM9 sobe,
        # venda quando o MM9 cai. Comportamento histórico padrão.
        trigger_up = df["SMA_9"] > df["SMA_9"].shift(1)
        trigger_down = df["SMA_9"] < df["SMA_9"].shift(1)
    elif trigger_mode == "level":
        # Direção pelo nível do preço vs MM9: compra quando o preço fica
        # acima do MM9, venda quando fica abaixo.
        trigger_up = df["Close"] > df["SMA_9"]
        trigger_down = df["Close"] < df["SMA_9"]
    elif trigger_mode == "cross":
        # Cruzamento real MM9 <-> MM21.
        trigger_up = (df["SMA_9"] > df["SMA_21"]) & (df["SMA_9"].shift(1) <= df["SMA_21"].shift(1))
        trigger_down = (df["SMA_9"] < df["SMA_21"]) & (df["SMA_9"].shift(1) >= df["SMA_21"].shift(1))
    elif trigger_mode == "pullback":
        # Retorno do preço à EMA: no viés de alta, o preço mergulha até a EMA
        # (zona de liquidez neutra) e fecha acima dela = confirmação para subir.
        # Só dispara no PONTO do retorno, não quando o preço corre longe da EMA.
        ema_col = f"EMA_{pullback_ema}"
        if ema_col not in df.columns:
            ema_col = "EMA_50"
        ema_line = df[ema_col]
        touch_low = df["Low"] <= ema_line
        touch_high = df["High"] >= ema_line
        close_above = df["Close"] > ema_line
        close_below = df["Close"] < ema_line
        # pullback de alta: preço toca a EMA por baixo e fecha acima → retomada
        trigger_up = touch_low & close_above
        # pullback de baixa: preço toca a EMA por cima e fecha abaixo → retomada
        trigger_down = touch_high & close_below
    elif trigger_mode == "3_9_20":
        # 3 cruza 9
        cross_up = (df["SMA_3"] > df["SMA_9"]) & (df["SMA_3"].shift(1) <= df["SMA_9"].shift(1))
        cross_down = (df["SMA_3"] < df["SMA_9"]) & (df["SMA_3"].shift(1) >= df["SMA_9"].shift(1))
        
        sma20_up = df["SMA_20"] > df["SMA_20"].shift(1)
        sma20_down = df["SMA_20"] < df["SMA_20"].shift(1)
        
        mm_under_candles = (df["Low"] > df["SMA_9"]) & (df["Low"] > df["SMA_20"])
        mm_over_candles = (df["High"] < df["SMA_9"]) & (df["High"] < df["SMA_20"])
        
        trigger_up = cross_up & sma20_up & mm_under_candles
        trigger_down = cross_down & sma20_down & mm_over_candles
    elif trigger_mode == "fimathe_tokio":
        df["CR_High"] = np.nan
        df["CR_Low"] = np.nan
        
        # Desloca o índice para agrupar o "dia" começando na fimathe_hour
        # Ex: se fimathe_hour=20, 20:00 de hj até 19:59 de amanhã vira o mesmo 'trade_day'
        shifted_idx = df.index - pd.Timedelta(hours=fimathe_hour)
        trade_day = shifted_idx.date
        
        for t_day, group in df.groupby(trade_day):
            # Pega os primeiros `fimathe_candles` do grupo
            formation_idx = group.head(fimathe_candles).index
            if len(formation_idx) > 0:
                c_high = group.loc[formation_idx, "High"].max()
                c_low = group.loc[formation_idx, "Low"].min()
                df.loc[group.index, "CR_High"] = c_high
                df.loc[group.index, "CR_Low"] = c_low

        # Calcula o tamanho original do canal
        channel_size = df["CR_High"] - df["CR_Low"]
        
        # Fatiamento: Se o canal for maior que max_pips, corta o tamanho dele pela metade
        max_size = fimathe_max_pips * pip_size
        oversized = channel_size > max_size
        if oversized.any():
            # Quando dividimos o canal de referência que ficou muito grande (fatiamento),
            # o tamanho do risco (distância do canal) cai pela metade, mas os preços de 
            # rompimento (CR_High e CR_Low) se mantém os mesmos.
            channel_size.loc[oversized] = channel_size.loc[oversized] / 2.0
        
        # Gatilho: fechamento do candle fora do canal de referência
        # Adicionamos um shift(1) no High/Low para garantir que o candle ANTERIOR estava dentro ou fechou rompendo agora
        trigger_up = (df["Close"] > df["CR_High"]) & (df["Close"].shift(1) <= df["CR_High"])
        trigger_down = (df["Close"] < df["CR_Low"]) & (df["Close"].shift(1) >= df["CR_Low"])
        
        # Calcula SL e TP Dinâmicos (2 níveis Fimathe)
        # Compra: ZN fica abaixo do CR. SL é 2 canais pra baixo da entrada. TP é 2 canais pra cima.
        df["Suggested_SL"] = np.nan
        df["Suggested_TP"] = np.nan
        
        # Preenche os limites dinâmicos APENAS nos momentos de gatilho
        df.loc[trigger_up, "Suggested_SL"] = df.loc[trigger_up, "Close"] - (2 * channel_size[trigger_up])
        df.loc[trigger_up, "Suggested_TP"] = df.loc[trigger_up, "Close"] + (2 * channel_size[trigger_up])
        
        df.loc[trigger_down, "Suggested_SL"] = df.loc[trigger_down, "Close"] + (2 * channel_size[trigger_down])
        df.loc[trigger_down, "Suggested_TP"] = df.loc[trigger_down, "Close"] - (2 * channel_size[trigger_down])
    elif trigger_mode == "clarezas":
        # ─── CLAREZAS: Fluxo + Absorção + Continuação ──────────────────────
        # Gatilho baseado em detecção de fluxo institucional:
        #   - Fluxo forte: volume > média*1.5 + candle direcional
        #   - Absorção: volume alto + candle pequeno (indecisão = acúmulo)
        #   - Continuação: fluxo + pullback + rompimento
        df["Suggested_SL"] = np.nan
        df["Suggested_TP"] = np.nan

        has_buy_flow = "Clarezas_BuyFlow" in df.columns
        has_sell_flow = "Clarezas_SellFlow" in df.columns
        has_absorption = "Clarezas_Absorption" in df.columns
        has_cont_buy = "Clarezas_ContinuationBuy" in df.columns
        has_cont_sell = "Clarezas_ContinuationSell" in df.columns

        if has_buy_flow and has_sell_flow:
            # Continuação é o sinal mais forte (fluxo + pullback + rompimento)
            trigger_up = df["Clarezas_ContinuationBuy"] if has_cont_buy else pd.Series(False, index=df.index)
            trigger_down = df["Clarezas_ContinuationSell"] if has_cont_sell else pd.Series(False, index=df.index)

            # Absorção é sinal secundário (confirma acumulação)
            if has_absorption:
                trigger_up = trigger_up | df["Clarezas_Absorption"]
                trigger_down = trigger_down | df["Clarezas_Absorption"]

            # SL/TP baseados em ATR
            if "ATR" in df.columns:
                atr_val = df["ATR"]
                df.loc[trigger_up, "Suggested_SL"] = df.loc[trigger_up, "Close"] - 1.5 * atr_val[trigger_up]
                df.loc[trigger_up, "Suggested_TP"] = df.loc[trigger_up, "Close"] + 3.0 * atr_val[trigger_up]
                df.loc[trigger_down, "Suggested_SL"] = df.loc[trigger_down, "Close"] + 1.5 * atr_val[trigger_down]
                df.loc[trigger_down, "Suggested_TP"] = df.loc[trigger_down, "Close"] - 3.0 * atr_val[trigger_down]
        else:
            trigger_up = pd.Series(False, index=df.index)
            trigger_down = pd.Series(False, index=df.index)
    elif trigger_mode == "pullback_liquidity":
        # ─── PULLBACK + LIQUIDEZ — delega ao módulo quantitativo ─────────────
        # build_signal() avalia SOMENTE o último candle fechado com todos os
        # filtros: ADX, pullback relativo ao ATR, sweep com buffer, qualidade
        # do candle e espaço até a liquidez contrária. O resultado popula
        # Suggested_SL/TP e as séries de gatilho para o frame inteiro.
        df["Suggested_SL"] = np.nan
        df["Suggested_TP"] = np.nan

        # Fallback vetorial (sem o módulo instalado): mantém comportamento anterior.
        if not _PLS_AVAILABLE:
            ema20 = df["EMA_20"]
            ema50 = df["EMA_50"]
            ema_trend_up = ema20 > ema50
            ema_trend_down = ema20 < ema50
            sweep_high = df["Sweep_High"] if "Sweep_High" in df.columns else pd.Series(False, index=df.index)
            sweep_low = df["Sweep_Low"] if "Sweep_Low" in df.columns else pd.Series(False, index=df.index)
            pullback_to_ema20_buy = df["Low"] <= ema20
            pullback_to_ema20_sell = df["High"] >= ema20
            rejection_buy = df["Close"] > ema20
            rejection_sell = df["Close"] < ema20
            atr_val = df["ATR"] if "ATR" in df.columns else pd.Series(0, index=df.index)
            volatility_ok = atr_val >= 3.5
            no_chop = (ema20 - ema50).abs() >= 0.5
            quality_filter = volatility_ok & no_chop
            trigger_up = ema_trend_up & sweep_low & pullback_to_ema20_buy & rejection_buy & quality_filter
            trigger_down = ema_trend_down & sweep_high & pullback_to_ema20_sell & rejection_sell & quality_filter
            prev_low = df["Low"].shift(1)
            prev_high = df["High"].shift(1)
            structure_stop_buy = pd.concat([prev_low, df["Low"]], axis=1).min(axis=1)
            atr_stop_buy = df["Close"] - atr_val
            suggested_sl_buy = pd.concat([structure_stop_buy, atr_stop_buy], axis=1).min(axis=1)
            structure_stop_sell = pd.concat([prev_high, df["High"]], axis=1).max(axis=1)
            atr_stop_sell = df["Close"] + atr_val
            suggested_sl_sell = pd.concat([structure_stop_sell, atr_stop_sell], axis=1).max(axis=1)
            risk_buy = (df["Close"] - suggested_sl_buy).abs()
            risk_sell = (suggested_sl_sell - df["Close"]).abs()
            df.loc[trigger_up, "Suggested_SL"] = suggested_sl_buy[trigger_up]
            df.loc[trigger_up, "Suggested_TP"] = df.loc[trigger_up, "Close"] + (2 * risk_buy[trigger_up])
            df.loc[trigger_down, "Suggested_SL"] = suggested_sl_sell[trigger_down]
            df.loc[trigger_down, "Suggested_TP"] = df.loc[trigger_down, "Close"] - (2 * risk_sell[trigger_down])
        else:
            # ── Motor quantitativo: avalia o candle fechado (-2) ───────────────
            df.attrs[SIGNAL_IDX_ATTR] = -2
            pls_cfg = PullbackLiquidityConfig(**(pls_config_kwargs or {}))
            pls_signal = _pls_build_signal(df, cfg=pls_cfg, use_closed_candle=True)

            # Monta séries de gatilho para o frame (apenas o último candle fechado)
            trigger_up = pd.Series(False, index=df.index)
            trigger_down = pd.Series(False, index=df.index)

            if pls_signal is not None:
                closed_idx = df.index[-2]  # candle que gerou o sinal
                if pls_signal.side == "buy":
                    trigger_up.loc[closed_idx] = True
                    df.loc[closed_idx, "Suggested_SL"] = pls_signal.stop
                    df.loc[closed_idx, "Suggested_TP"] = pls_signal.take
                    df.loc[closed_idx, "Signal"] = 1
                else:
                    trigger_down.loc[closed_idx] = True
                    df.loc[closed_idx, "Suggested_SL"] = pls_signal.stop
                    df.loc[closed_idx, "Suggested_TP"] = pls_signal.take
                    df.loc[closed_idx, "Signal"] = -1

            # Guarda o sinal estruturado para uso no bloco de reason abaixo
            df.attrs["_pls_signal"] = pls_signal
    else:
        raise ValueError(f"trigger_mode desconhecido: {trigger_mode!r}")

    rsi_not_overbought = df["RSI"] < rsi_overbought
    rsi_not_oversold = df["RSI"] > rsi_oversold

    confirm = _confirmation_column(df, effective_use_volume)

    if min_adx and "ADX" in df.columns:
        strong_trend = df["ADX"].fillna(0) >= min_adx
    else:
        strong_trend = pd.Series(True, index=df.index)

    buy = trend_up & trigger_up & rsi_not_overbought & confirm & session_ok & strong_trend
    sell = trend_down & trigger_down & rsi_not_oversold & confirm & session_ok & strong_trend

    # Preserva sinal já setado pelo PLS (candle [-2]) antes da atribuição vetorial
    _pls_preserved = df["Signal"].copy()

    df.loc[buy, "Signal"] = 1
    df.loc[sell, "Signal"] = -1

    # Restaura sinal PLS que foi gerado em candle fechado (não sobrepor),
    # MAS submetendo-o aos MESMOS filtros dos demais gatilhos. Antes desta
    # correção o sinal PLS era restaurado incondicionalmente — os flags
    # --session, --min-adx e --rsi-ob/os do cron não filtravam nada.
    if trigger_mode == "pullback_liquidity":
        _pls_mask = _pls_preserved != 0
        if _pls_mask.any():
            _pls_buy = _pls_mask & (_pls_preserved == 1)
            _pls_sell = _pls_mask & (_pls_preserved == -1)
            _pls_ok = (
                session_ok & strong_trend & confirm
                & ((_pls_buy & rsi_not_overbought) | (_pls_sell & rsi_not_oversold))
            )
            _pls_ok = _pls_ok.fillna(False)
            df.loc[_pls_mask & ~_pls_ok, "Signal"] = 0
            _keep = _pls_mask & _pls_ok
            df.loc[_keep, "Signal"] = _pls_preserved[_keep]
            df.attrs["pls_filtered_rows"] = int((_pls_mask & ~_pls_ok).sum())

    # ── Gate global de volatilidade (todos os gatilhos) ─────────────────────
    # Zera o sinal onde o regime está EXTREMO (rajada/news) ou o mercado está
    # em lateral pura (chop) — os dois cenários que geram stop atrás do stop.
    # Percentil (Vol_Percentile) e ER (Efficiency Ratio) são relativos ao
    # próprio ativo/timeframe: valem para ouro, forex e índices sem calibrar.
    df.attrs["vol_block_msg"] = ""
    if "Vol_Percentile" in df.columns:
        if max_vol_percentile and float(max_vol_percentile) > 0:
            vol_bad = df["Vol_Percentile"] > float(max_vol_percentile)
        else:
            vol_bad = pd.Series(False, index=df.index)
        if min_efficiency_ratio and float(min_efficiency_ratio) > 0 and "Efficiency_Ratio" in df.columns:
            vol_bad = vol_bad | (df["Efficiency_Ratio"] < float(min_efficiency_ratio))
        vol_bad = vol_bad.fillna(False).astype(bool)

        _ai = signal_index(df, trigger_mode)
        _sig_ai = int(df["Signal"].iloc[_ai])
        if _sig_ai != 0 and bool(vol_bad.iloc[_ai]):
            parts = []
            _vp = df["Vol_Percentile"].iloc[_ai]
            if pd.notna(_vp) and _vp > float(max_vol_percentile or 0):
                parts.append(f"volatilidade extrema (P{float(_vp):.0f})")
            if "Efficiency_Ratio" in df.columns:
                _er = df["Efficiency_Ratio"].iloc[_ai]
                if pd.notna(_er) and _er < float(min_efficiency_ratio or 0):
                    parts.append(f"lateral pura (ER {float(_er):.2f})")
            df.attrs["vol_block_msg"] = " · ".join(parts) or "regime de volatilidade fora do limite"
        df.loc[vol_bad, "Signal"] = 0

    # ── Motivo do sinal + R:R (para decisão manual) ──────────────────────
    df.attrs["signal_reason"] = ""
    df.attrs["signal_rr"] = None
    df.attrs["signal_entry"] = None
    df.attrs["signal_sl"] = None
    df.attrs["signal_tp"] = None

    last_idx = df.index[signal_index(df, trigger_mode)]
    sig = int(df.loc[last_idx, "Signal"])
    if sig != 0:
        reasons = []
        # Tendência
        if sig == 1:
            if "EMA_20" in df.columns and "EMA_50" in df.columns:
                if df.loc[last_idx, "EMA_20"] > df.loc[last_idx, "EMA_50"]:
                    reasons.append("EMA20>EMA50 (alta)")
            if df.loc[last_idx, "Close"] > df.loc[last_idx, "SMA_200"]:
                reasons.append("preço>MM200")
        else:
            if "EMA_20" in df.columns and "EMA_50" in df.columns:
                if df.loc[last_idx, "EMA_20"] < df.loc[last_idx, "EMA_50"]:
                    reasons.append("EMA20<EMA50 (baixa)")
            if df.loc[last_idx, "Close"] < df.loc[last_idx, "SMA_200"]:
                reasons.append("preço<MM200")

        # Gatilho
        if trigger_mode == "pullback_liquidity":
            # Usa o reason estruturado do módulo quantitativo quando disponível.
            pls_sig = df.attrs.get("_pls_signal")
            if pls_sig is not None and _PLS_AVAILABLE:
                reasons.append(pls_sig.reason)
                reasons.append(f"ADX {pls_sig.adx:.0f}")
                reasons.append(f"ATR {pls_sig.atr:.4f}")
                reasons.append(f"R:R {pls_sig.rr:.1f}")
                reasons.append(f"sweep @ {pls_sig.sweep_extreme:.4f}")
            else:
                if sig == 1:
                    if "Sweep_High" in df.columns and df.loc[last_idx, "Sweep_High"]:
                        reasons.append("sweep de liquidez (high varrido)")
                    if "EMA_20" in df.columns and df.loc[last_idx, "Low"] <= df.loc[last_idx, "EMA_20"]:
                        reasons.append("pullback na EMA20")
                    if df.loc[last_idx, "Close"] > df.loc[last_idx, "EMA_20"]:
                        reasons.append("rejeição (close>EMA20)")
                else:
                    if "Sweep_Low" in df.columns and df.loc[last_idx, "Sweep_Low"]:
                        reasons.append("sweep de liquidez (low varrido)")
                    if "EMA_20" in df.columns and df.loc[last_idx, "High"] >= df.loc[last_idx, "EMA_20"]:
                        reasons.append("pullback na EMA20")
                    if df.loc[last_idx, "Close"] < df.loc[last_idx, "EMA_20"]:
                        reasons.append("rejeição (close<EMA20)")
        elif trigger_mode == "pullback":
            ema_col = f"EMA_{pullback_ema}" if f"EMA_{pullback_ema}" in df.columns else "EMA_50"
            if sig == 1:
                reasons.append("pullback à EMA (toque + bounce)")
            else:
                reasons.append("pullback à EMA (toque + rejeição baixa)")
        elif trigger_mode == "cross":
            if sig == 1:
                reasons.append("cruzamento MM9>MM21 (golden cross)")
            else:
                reasons.append("cruzamento MM9<MM21 (death cross)")
        elif trigger_mode == "slope":
            if sig == 1:
                reasons.append("MM9 inclinando pra cima")
            else:
                reasons.append("MM9 inclinando pra baixo")
        elif trigger_mode == "level":
            if sig == 1:
                reasons.append("preço acima do MM9")
            else:
                reasons.append("preço abaixo do MM9")
        elif trigger_mode == "3_9_20":
            if sig == 1:
                reasons.append("MM3 cruza MM9 + MM20 subindo + candle acima")
            else:
                reasons.append("MM3 cruza MM9 + MM20 caindo + candle abaixo")
        elif trigger_mode == "fimathe_tokio":
            if sig == 1:
                reasons.append("rompimento acima do Canal de Referência")
            else:
                reasons.append("rompimento abaixo do Canal de Referência")
        elif trigger_mode == "clarezas":
            if sig == 1:
                if "Clarezas_ContinuationBuy" in df.columns and df.loc[last_idx, "Clarezas_ContinuationBuy"]:
                    reasons.append("continuação de alta (fluxo+pullback+rompimento)")
                elif "Clarezas_Absorption" in df.columns and df.loc[last_idx, "Clarezas_Absorption"]:
                    reasons.append("absorção de compra (volume alto+candle pequeno)")
                elif "Clarezas_BuyFlow" in df.columns and df.loc[last_idx, "Clarezas_BuyFlow"]:
                    reasons.append("fluxo de compra forte (volume+direção)")
            else:
                if "Clarezas_ContinuationSell" in df.columns and df.loc[last_idx, "Clarezas_ContinuationSell"]:
                    reasons.append("continuação de baixa (fluxo+pullback+rompimento)")
                elif "Clarezas_Absorption" in df.columns and df.loc[last_idx, "Clarezas_Absorption"]:
                    reasons.append("absorção de venda (volume alto+candle pequeno)")
                elif "Clarezas_SellFlow" in df.columns and df.loc[last_idx, "Clarezas_SellFlow"]:
                    reasons.append("fluxo de venda forte (volume+direção)")

        # Filtros
        if "RSI" in df.columns:
            reasons.append(f"RSI {df.loc[last_idx, 'RSI']:.0f}")
        if "ADX" in df.columns:
            reasons.append(f"ADX {df.loc[last_idx, 'ADX']:.0f}")
        if "Vol_Percentile" in df.columns and pd.notna(df.loc[last_idx, "Vol_Percentile"]):
            _vp = float(df.loc[last_idx, "Vol_Percentile"])
            _vr = str(df.loc[last_idx].get("Vol_Regime") or "")
            reasons.append(f"vol P{_vp:.0f}{(' ' + _vr) if _vr else ''}")
        if session:
            reasons.append(f"sessão {session}")

        df.attrs["signal_reason"] = " · ".join(reasons)

        # R:R
        entry = float(df.loc[last_idx, "Close"])
        sl_val = df.loc[last_idx].get("Suggested_SL")
        tp_val = df.loc[last_idx].get("Suggested_TP")
        if pd.notna(sl_val) and pd.notna(tp_val):
            risk = abs(entry - float(sl_val))
            reward = abs(float(tp_val) - entry)
            rr = reward / risk if risk > 0 else 0
            df.attrs["signal_rr"] = round(rr, 2)
            df.attrs["signal_entry"] = round(entry, 5)
            df.attrs["signal_sl"] = round(float(sl_val), 5)
            df.attrs["signal_tp"] = round(float(tp_val), 5)

    if live is not None:
        df = apply_live_confirmation(
            df, quote=live,
            max_spread_pips=max_spread_pips,
            min_tick_volume=min_tick_volume,
            pip_size=pip_size,
            tz_offset_hours=tz_offset_hours,
        )
    return df


def train_ml_model(df: pd.DataFrame, n_splits: int = 4):
    """
    Treina RandomForest com validação WALK-FORWARD (janela móvel temporal).

    Diferente do split único (que testa só num pedaço final), aqui o histórico é
    dividido em `n_splits` janelas e o modelo é re-treinado a cada passo, testando
    SEMPRE em dados que o treino nunca viu. A acurácia reportada é a média
    honesta dessas janelas — muito menos propensa a iludir.

    Retorna: model (treinado no conjunto completo), metrics, (X_test, y_test).
    """
    data = df.dropna(subset=FEATURE_COLUMNS + ["Direction"]).copy()

    if len(data) < 50:
        raise ValueError(
            f"Dados insuficientes para treinar com confiança ({len(data)} linhas). "
            "Aumente o período ou reduza o timeframe."
        )

    X = data[FEATURE_COLUMNS]
    y = data["Direction"]

    n = len(data)
    n_splits = max(2, min(n_splits, n // 20))  # garante janelas com mínimo de dados
    fold = n // n_splits

    # ---- walk-forward: treina no passado, testa no futuro de cada janela ----
    wf_accs, wf_baselines, wf_models = [], [], []
    for k in range(1, n_splits):
        tr_end = k * fold
        te_end = (k + 1) * fold
        X_tr, X_te = X.iloc[:tr_end], X.iloc[tr_end:te_end]
        y_tr, y_te = y.iloc[:tr_end], y.iloc[tr_end:te_end]
        if len(X_te) < 20 or y_tr.nunique() < 2 or y_te.nunique() < 2:
            continue

        m = RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=10, random_state=42)
        m.fit(X_tr, y_tr)
        wf_models.append(m)
        wf_accs.append(accuracy_score(y_te, m.predict(X_te)))
        wf_baselines.append(max(y_te.mean(), 1 - y_te.mean()))

    if not wf_accs:
        raise ValueError("Histórico curto demais para validar walk-forward. Aumente o período.")

    wf_accuracy = float(np.mean(wf_accs))
    wf_baseline = float(np.mean(wf_baselines))
    wf_std = float(np.std(wf_accs))
    wf_edge = wf_accuracy - wf_baseline  # ganho real sobre "chutar o lado mais comum"

    # ---- modelo final treinado em TODO o histórico (para a previsão ao vivo) ----
    model = RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=10, random_state=42)
    model.fit(X, y)

    # relatório detalhado na última janela (a mais recente)
    last_model = wf_models[-1]
    X_tr, X_te = X.iloc[: n - fold], X.iloc[n - fold:]
    y_tr, y_te = y.iloc[: n - fold], y.iloc[n - fold:]
    y_pred = last_model.predict(X_te)
    report = classification_report(y_te, y_pred, output_dict=True, zero_division=0)

    metrics = {
        "accuracy": wf_accuracy,
        "baseline": wf_baseline,
        "wf_std": wf_std,
        "wf_edge": wf_edge,
        "n_splits": len(wf_accs) + 1,
        "report": report,
    }
    return model, metrics, (X_te, y_te)


def predict_latest(model, df: pd.DataFrame):
    """
    Prevê usando a ÚLTIMA linha fechada (a linha em formação não tem 'Direction').
    Retorna (pred, margin) onde margin = |p - 0.5| é a confiança HONESTA:
    margin < 0.05 => moeda lançada. margem > 0.2 => sinal com alguma informação.
    """
    row = df.dropna(subset=FEATURE_COLUMNS).iloc[[-1]][FEATURE_COLUMNS]
    pred = model.predict(row)[0]
    proba = model.predict_proba(row)
    p = float(proba[0][list(model.classes_).index(pred)]) if len(model.classes_) > 1 else 0.5
    margin = abs(p - 0.5)
    return pred, margin, p
