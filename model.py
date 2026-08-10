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


def _is_in_session(index: pd.DatetimeIndex, session: str) -> pd.Series:
    """Retorna máscara booleana: quais candles estão dentro da sessão escolhida (UTC)."""
    hours = index.hour  # index já vem em UTC da AwesomeAPI; para yfinance convertemos antes
    if session not in SESSION_UTC:
        return pd.Series(True, index=index)
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
                            pip_size: Optional[float] = None) -> pd.DataFrame:
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

        tick_vol = float(live.get("tick_volume") or 0.0)
        day_vol = float(live.get("day_volume") or 0.0)
        if min_tick_volume > 0:
            if tick_vol > 0 and day_vol > 0 and tick_vol < min_tick_volume:
                reasons.append(f"volume tick {tick_vol:.0f} < {min_tick_volume:.0f}")
            elif day_vol <= 0:
                reasons.append("mercado sem volume do dia (fechado/stop)")

        if reasons:
            df.attrs["signal_block_msg"] = " · ".join(reasons)
            return 0
        df.attrs["signal_block_msg"] = ""
        return signal

    if "Signal" in df.columns and len(df) > 0:
        df.loc[df.index[-1], "Signal"] = _apply(int(df.loc[df.index[-1], "Signal"]), quote)
    return df


def generate_rule_based_signals(
    df: pd.DataFrame,
    rsi_overbought: float = 70,
    rsi_oversold: float = 30,
    use_volume: bool = True,
    use_cross: bool = True,
    trigger_mode: str = "slope",
    pullback_ema: int = 50,
    session: str = "",
    min_adx: float = 0.0,
    live=None,
    max_spread_pips: float = 0.0,
    min_tick_volume: float = 0.0,
    pip_size: Optional[float] = None,
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
    """
    df = df.copy()
    df["Signal"] = 0

    # normaliza index p/ UTC (yfinance entrega tz-aware; AwesomeAPI já é UTC)
    index_utc = df.index
    if isinstance(index_utc, pd.DatetimeIndex) and getattr(index_utc, "tz", None) is not None:
        index_utc = index_utc.tz_convert("UTC")
    session_ok = _is_in_session(index_utc, session)

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
        # Filtro de tempo: das 20:00 às 20:15 (4 candles de 5 minutos)
        # O dia de "trading" do forex costuma virar as 17:00 EST. 
        # Identificamos a janela de formação do canal (CR)
        is_formation = (df.index.hour == 20) & (df.index.minute < 20)
        
        # Cria colunas temporárias para carregar a Máxima e Mínima do canal de referência pro resto do dia
        df["CR_High"] = np.nan
        df["CR_Low"] = np.nan
        
        # Agrupa pelo dia (para garantir que só temos um canal por dia, a partir das 20:00)
        # Como 20:00 já é noite, o "dia de trade" vai das 20:00 de um dia até 19:59 do outro.
        # Vamos criar um "trade_day" que muda às 20:00
        trade_day = (df.index.hour >= 20).astype(int).cumsum() - (df.index.hour >= 20).astype(int) + df.index.dayofyear
        
        for t_day, group in df.groupby(trade_day):
            formation_idx = group[group.index.hour == 20][group.index.minute < 20].index
            if len(formation_idx) > 0:
                c_high = group.loc[formation_idx, "High"].max()
                c_low = group.loc[formation_idx, "Low"].min()
                # Preenche o resto do dia com esses valores
                df.loc[group.index, "CR_High"] = c_high
                df.loc[group.index, "CR_Low"] = c_low

        # Calcula o tamanho do canal
        channel_size = df["CR_High"] - df["CR_Low"]
        
        # Gatilho: fechamento do candle fora do canal de referência
        # Adicionamos um shift(1) no High/Low para garantir que o candle ANTERIOR estava dentro ou fechou rompendo agora
        trigger_up = (df["Close"] > df["CR_High"]) & (df["Close"].shift(1) <= df["CR_High"])
        trigger_down = (df["Close"] < df["CR_Low"]) & (df["Close"].shift(1) >= df["CR_Low"])
        
        # Desliga as médias para essa estratégia, o price action é soberano
        trend_up = pd.Series(True, index=df.index)
        trend_down = pd.Series(True, index=df.index)
        
        # Calcula SL e TP Dinâmicos (2 níveis Fimathe)
        # Compra: ZN fica abaixo do CR. SL é 2 canais pra baixo da entrada. TP é 2 canais pra cima.
        df["Suggested_SL"] = np.nan
        df["Suggested_TP"] = np.nan
        
        # Preenche os limites dinâmicos APENAS nos momentos de gatilho
        df.loc[trigger_up, "Suggested_SL"] = df.loc[trigger_up, "Close"] - (2 * channel_size[trigger_up])
        df.loc[trigger_up, "Suggested_TP"] = df.loc[trigger_up, "Close"] + (2 * channel_size[trigger_up])
        
        df.loc[trigger_down, "Suggested_SL"] = df.loc[trigger_down, "Close"] + (2 * channel_size[trigger_down])
        df.loc[trigger_down, "Suggested_TP"] = df.loc[trigger_down, "Close"] - (2 * channel_size[trigger_down])
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

    df.loc[buy, "Signal"] = 1
    df.loc[sell, "Signal"] = -1

    if live is not None:
        df = apply_live_confirmation(
            df, quote=live,
            max_spread_pips=max_spread_pips,
            min_tick_volume=min_tick_volume,
            pip_size=pip_size,
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
