"""
Indicadores técnicos, implementados sem dependência externa (pandas_ta),
para evitar problema de instalação e para eu poder garantir a fórmula exata
que você está acostumado a ver na IQ Option / MetaTrader.
"""

import pandas as pd
import numpy as np
from typing import Optional

# Volatilidade precisa (Parkinson/Garman-Klass/Yang-Zhang + regime + stop adaptativo)
try:
    from volatility import add_volatility as _add_volatility, vol_summary as _vol_summary
    _VOL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _VOL_AVAILABLE = False
    _add_volatility = None
    _vol_summary = None


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """
    RSI com suavização de Wilder (o padrão real do indicador, usado pela
    IQ Option e MetaTrader) — diferente de usar rolling().mean() simples,
    que gera valores divergentes do que você vê na plataforma.
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.fillna(100)  # quando avg_loss = 0, RSI = 100 por definição
    # mercado totalmente parado (gain=loss=0): RSI neutro = 50, não 100
    flat = (avg_gain == 0) & (avg_loss == 0)
    result = result.mask(flat, 50.0)
    return result


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def bollinger_bands(series: pd.Series, length: int = 20, std_mult: float = 2.0):
    mid = sma(series, length)
    std = series.rolling(window=length).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """
    ADX (Wilder) — mede a FORÇA da tendência, não a direção.
    > 25 costuma indicar tendência forte; < 20 indica mercado lateral (range).
    Usado como filtro para só operar quando há tendência aproveitável.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    # True Range + suavização de Wilder
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / length, adjust=False).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def find_liquidity_pools(df: pd.DataFrame) -> pd.DataFrame:
    """Encontra FVGs (Fair Value Gaps) de SMC para mapear poços de liquidez."""
    # Inicializa as colunas
    for col in ["FVG_Bull_Top", "FVG_Bull_Bot", "FVG_Bear_Top", "FVG_Bear_Bot"]:
        if col not in df.columns:
            df[col] = np.nan

    # FVG Bullish: Gap de alta (Low atual > High de 2 candles atrás)
    bull_fvg = df["Low"] > df["High"].shift(2)
    df.loc[bull_fvg, "FVG_Bull_Top"] = df["Low"]
    df.loc[bull_fvg, "FVG_Bull_Bot"] = df["High"].shift(2)

    # FVG Bearish: Gap de baixa (High atual < Low de 2 candles atrás)
    bear_fvg = df["High"] < df["Low"].shift(2)
    df.loc[bear_fvg, "FVG_Bear_Top"] = df["Low"].shift(2)
    df.loc[bear_fvg, "FVG_Bear_Bot"] = df["High"]
    
    return df

def get_active_fvgs(df: pd.DataFrame, max_lookback: int = 150):
    """
    Retorna listas de gaps de compra e venda não mitigados.
    Um gap é mitigado quando o preço o preenche completamente.

    Cada gap carrega também `age`: quantidade de candles desde que foi formado.
    Quanto mais novo (fresco), mais forte a zona de liquidez em SMC.
    """
    recent = df.iloc[-max_lookback:] if len(df) > max_lookback else df
    
    bull_gaps = []
    bear_gaps = []
    last_pos = len(recent) - 1
    
    for pos, (idx, row) in enumerate(recent.iterrows()):
        # 1. Conta toques: transição de FORA -> DENTRO da zona. Permanecer vários
        #    candles dentro dela conta como UM toque (evita inflar o número).
        for g in bull_gaps:
            inside = row["Low"] <= g["top"]
            if inside and not g.get("_in", False):
                g["touches"] += 1
            g["_in"] = inside
        for g in bear_gaps:
            inside = row["High"] >= g["bot"]
            if inside and not g.get("_in", False):
                g["touches"] += 1
            g["_in"] = inside

        # 2. Se achou um gap, adiciona na lista
        if pd.notna(row.get("FVG_Bull_Top")):
            bull_gaps.append({"top": row["FVG_Bull_Top"], "bot": row["FVG_Bull_Bot"], "time": idx, "age": last_pos - pos, "touches": 0, "_in": True})
        if pd.notna(row.get("FVG_Bear_Top")):
            bear_gaps.append({"top": row["FVG_Bear_Top"], "bot": row["FVG_Bear_Bot"], "time": idx, "age": last_pos - pos, "touches": 0, "_in": True})
            
        # 3. Remove gaps que foram mitigados (preenchidos pelo preço)
        #    Um gap de compra continua ativo enquanto o preço não cair abaixo do seu fundo
        bull_gaps = [g for g in bull_gaps if row["Low"] > g["bot"]]
        # Um gap de venda continua ativo enquanto o preço não subir acima do seu topo
        bear_gaps = [g for g in bear_gaps if row["High"] < g["top"]]
        
    return bull_gaps, bear_gaps


def fvg_strength(gap: dict) -> str:
    """Força de uma zona de liquidez (FVG) baseada na liquidez.

    Frescor (idade) conta a favor: zona nova e pouco testada é a mais forte.
    Toques contam contra: cada reteste que o preço faz gasta a liquidez —
    com 3+ toques a zona perde força e o risco de quebrar (atravessar) é alto.
    """
    age = int(gap.get("age", 999))
    touches = int(gap.get("touches", 0))
    if touches >= 3:
        return "FRACA"
    if touches == 2:
        return "MÉDIA"
    if age <= 5:
        return "MUITO FORTE"
    if age <= 15:
        return "FORTE"
    if age <= 40:
        return "MÉDIA"
    return "FRACA"

def find_swing_liquidity(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Encontra Swing Highs (BSL) e Swing Lows (SSL) para mapear liquidez de varejo (stops)."""
    # Fractal básico: o topo/fundo é o maior/menor de uma janela de X candles (ex: 5)
    df["Swing_High"] = df["High"] == df["High"].rolling(window=window, center=True).max()
    df["Swing_Low"] = df["Low"] == df["Low"].rolling(window=window, center=True).min()
    return df

def get_active_swings(df: pd.DataFrame, max_lookback: int = 150):
    """Retorna a liquidez externa (Buy-Side e Sell-Side) não capturada."""
    recent = df.iloc[-max_lookback:] if len(df) > max_lookback else df
    bsl = []
    ssl = []
    
    for idx, row in recent.iterrows():
        # 1. Mitigação (Sweep): Remove os stops que já foram varridos pelo preço atual
        bsl = [s for s in bsl if row["High"] < s["price"]]
        ssl = [s for s in ssl if row["Low"] > s["price"]]

        # 2. Adiciona novos topos/fundos identificados neste candle
        if row.get("Swing_High") == True:
            bsl.append({"price": row["High"], "time": idx})
        if row.get("Swing_Low") == True:
            ssl.append({"price": row["Low"], "time": idx})
        
    return bsl, ssl


def swing_distance(df: pd.DataFrame, price: float, pip_size: Optional[float] = None) -> dict:
    """Distância até a liquidez mais próxima do preço atual.

    Retorna a próxima BSL (liquidez de compra, acima) e SSL (liquidez de venda,
    abaixo) com a distância em pips — a leitura direta de "para onde o mercado
    provavelmente vai buscar liquidez".
    """
    bsl, ssl = get_active_swings(df)
    above = [b["price"] for b in bsl if b["price"] > price]
    below = [s["price"] for s in ssl if s["price"] < price]
    next_bsl = min(above) if above else None
    next_ssl = max(below) if below else None

    def _pips(level: Optional[float]) -> Optional[float]:
        if level is None:
            return None
        d = abs(price - level)
        return d / pip_size if pip_size else d

    return {
        "next_bsl": next_bsl,
        "next_ssl": next_ssl,
        "bsl_pips": _pips(next_bsl),
        "ssl_pips": _pips(next_ssl),
    }

def get_current_wave(df: pd.DataFrame, max_lookback: int = 150):
    """
    Analisa o movimento (onda primária) recente e calcula retrações.
    Identifica o topo (Máxima) e o fundo (Mínima) dos últimos `max_lookback` candles.
    """
    recent = df.iloc[-max_lookback:] if len(df) > max_lookback else df
    if recent.empty:
        return None
        
    high_val = float(recent["High"].max())
    high_idx = recent["High"].idxmax()
    
    low_val = float(recent["Low"].min())
    low_idx = recent["Low"].idxmin()
    
    current_price = float(df["Close"].iloc[-1])
    
    # Evita erro se min/max forem iguais
    if high_val == low_val:
        return None
        
    # Define direção da onda (baseado em quem aconteceu primeiro)
    if low_idx < high_idx:
        direction = "Alta"
        origin = low_val
        extreme = high_val
        # Retração cai a partir do topo
        ret_33 = extreme - (extreme - origin) * 0.333
        ret_50 = extreme - (extreme - origin) * 0.50
        ret_66 = extreme - (extreme - origin) * 0.666
        ret_80 = extreme - (extreme - origin) * 0.80
    else:
        direction = "Baixa"
        origin = high_val
        extreme = low_val
        # Retração sobe a partir do fundo
        ret_33 = extreme + (origin - extreme) * 0.333
        ret_50 = extreme + (origin - extreme) * 0.50
        ret_66 = extreme + (origin - extreme) * 0.666
        ret_80 = extreme + (origin - extreme) * 0.80
        
    total_pct = abs(extreme - origin) / origin * 100
    
    current_time = df.index[-1]
    day_df = df[df.index.date == current_time.date()]
    if not day_df.empty:
        day_open = float(day_df["Open"].iloc[0])
        day_pct = (current_price - day_open) / day_open * 100
    else:
        day_pct = 0.0
    
    return {
        "direction": direction,
        "origin": origin,
        "extreme": extreme,
        "total_pct": total_pct,
        "day_pct": day_pct,
        "level_33": ret_33,
        "level_50": ret_50,
        "level_66": ret_66,
        "level_80": ret_80,
        "start_time": min(low_idx, high_idx),
        "end_time": max(low_idx, high_idx)
    }

def find_sweeps(df: pd.DataFrame, window: int = 5, lookback: int = 3) -> list:
    """Eventos de varredura de liquidez (sweep/fakeout) nos últimos candles.

    Um sweep acontece quando o preço atravessa um BSL/SSL com o pavio (wick)
    mas FECHA de volta — é o "spring" institucional (busca de liquidez + reversão).
    Só interessa o evento que acabou de acontecer (últimos `lookback` candles).
    """
    if "Swing_High" not in df.columns:
        df = find_swing_liquidity(df, window=window)
    bsl: list = []
    ssl: list = []
    events: list = []
    n = len(df)
    for i, t in enumerate(df.itertuples()):
        # 1. Checa sweep ANTES de mitigar (o sweep usa o nível ainda ativo)
        if i >= n - lookback:
            for s in bsl:
                if s["price"] <= t.High and s["price"] > t.Close:
                    events.append({"type": "BSL", "price": s["price"], "time": t.Index})
            for s in ssl:
                if s["price"] >= t.Low and s["price"] < t.Close:
                    events.append({"type": "SSL", "price": s["price"], "time": t.Index})
        # 2. Mitigação: nível sai da lista quando o preço cruza de verdade
        bsl = [s for s in bsl if t.High < s["price"]]
        ssl = [s for s in ssl if t.Low > s["price"]]
        # 3. Novos topos/fundos entram na lista de liquidez
        if bool(getattr(t, "Swing_High", False)):
            bsl.append({"price": float(t.High), "time": t.Index})
        if bool(getattr(t, "Swing_Low", False)):
            ssl.append({"price": float(t.Low), "time": t.Index})
    return events


def detect_equal_highs_lows(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """Detecta máximas e mínimas iguais (ou muito próximas) — zonas de liquidez.

    Equal Highs: máximas dentro de 0.5×ATR da máxima máxima no lookback → liquidez acima (BSL).
    Equal Lows:  mínimas dentro de 0.5×ATR da mínima mínima no lookback → liquidez abaixo (SSL).
    """
    atr_val = atr(df["High"], df["Low"], df["Close"], 14)
    tolerance = (atr_val * 0.5).fillna(method="ffill")

    rolling_max = df["High"].rolling(lookback, min_periods=1).max()
    rolling_min = df["Low"].rolling(lookback, min_periods=1).min()

    df["Equal_High"] = (df["High"] >= rolling_max - tolerance) & (df["High"] <= rolling_max + tolerance)
    df["Equal_Low"] = (df["Low"] <= rolling_min + tolerance) & (df["Low"] >= rolling_min - tolerance)
    return df


def detect_sweep_candle(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """Detecta se o candle atual fez sweep de liquidez (varreu stops e voltou).

    Sweep High (para BUY): High > máxima recente, mas Close volta pra dentro.
    Sweep Low  (para SELL): Low < mínima recente, mas Close volta pra dentro.
    """
    recent_high = df["High"].shift(1).rolling(lookback, min_periods=1).max()
    recent_low = df["Low"].shift(1).rolling(lookback, min_periods=1).min()

    df["Sweep_High"] = (df["High"] > recent_high) & (df["Close"] < recent_high)
    df["Sweep_Low"] = (df["Low"] < recent_low) & (df["Close"] > recent_low)
    return df


# ======================== CLAREZAS: Fluxo + Absorção + Continuação ========================

def clarezas_flow(df: pd.DataFrame, period: int = 20, multiplier: float = 1.5) -> pd.DataFrame:
    """Detecta fluxo forte de compra/venda: volume alto + direção do candle.

    StrongBuyFlow: volume > media * multiplier E close > open
    StrongSellFlow: volume > media * multiplier E close < open
    """
    vol_ma = sma(df["Volume"], period)
    high_vol = df["Volume"] > vol_ma * multiplier

    bullish = df["Close"] > df["Open"]
    bearish = df["Close"] < df["Open"]

    df["Clarezas_BuyFlow"] = high_vol & bullish
    df["Clarezas_SellFlow"] = high_vol & bearish
    return df


def clarezas_absorption(df: pd.DataFrame, period: int = 20, multiplier: float = 1.5,
                        body_ratio: float = 0.3) -> pd.DataFrame:
    """Detecta absorção institucional: volume alto + candle pequeno (indecisão).

    Alto volume + corpo pequeno relative ao range = grandes ordens sendo
    absorvidas sem mover o preço (acúmulo institucional).
    """
    vol_ma = sma(df["Volume"], period)
    high_vol = df["Volume"] > vol_ma * multiplier

    body = (df["Close"] - df["Open"]).abs()
    rng = df["High"] - df["Low"]
    rng = rng.replace(0, np.nan)
    small_body = (body / rng) < body_ratio

    df["Clarezas_Absorption"] = high_vol & small_body
    return df


def clarezas_continuation(df: pd.DataFrame, period: int = 20, multiplier: float = 1.5,
                          body_ratio: float = 0.3) -> pd.DataFrame:
    """Detecta continuação após pullback: fluxo forte + correção + rompimento.

    ContinuationBuy: fluxo de alta + candle anterior de baixa (pullback)
                     + candle atual fecha acima da máxima do pullback.
    ContinuationSell: fluxo de baixa + candle anterior de alta (pullback)
                      + candle atual fecha abaixo da mínima do pullback.
    """
    vol_ma = sma(df["Volume"], period)
    high_vol = df["Volume"] > vol_ma * multiplier

    # Fluxo forte (fecha na direção com volume alto)
    buy_flow = high_vol & (df["Close"] > df["Open"])
    sell_flow = high_vol & (df["Close"] < df["Open"])

    # Pullback: candle anterior oposto ao fluxo
    prev_bearish = df["Close"].shift(1) < df["Open"].shift(1)
    prev_bullish = df["Close"].shift(1) > df["Open"].shift(1)

    # Rompimento: candle atual quebra a extremidade do pullback
    break_high = df["Close"] > df["High"].shift(1)
    break_low = df["Close"] < df["Low"].shift(1)

    df["Clarezas_ContinuationBuy"] = buy_flow & prev_bearish & break_high
    df["Clarezas_ContinuationSell"] = sell_flow & prev_bullish & break_low
    return df


def build_volume_profile(df: pd.DataFrame, bins: int = 40):
    """Perfil de volume (estilo Profit Pro): volume negociado por nível de preço.

    Como só temos OHLC + tick_volume por candle (não tempo-at-price real), o volume
    é distribuído uniformemente pelo range do candle (Low→High), com peso dobrado no
    fechamento — aproximação razoável do perfil do broker. O último candle (em
    formação) entra com o volume acumulado até agora, então o perfil "cresce" ao vivo.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    vol = df["Volume"].fillna(0)
    if float(vol.sum()) <= 0:
        return None
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    if hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    mid = (edges[:-1] + edges[1:]) / 2.0
    profile = np.zeros(bins)
    span_total = hi - lo
    for _, row in df.iterrows():
        v = float(row["Volume"]) or 0.0
        if v <= 0:
            continue
        i_lo = int(np.clip((float(row["Low"]) - lo) / span_total * bins, 0, bins - 1))
        i_hi = int(np.clip((float(row["High"]) - lo) / span_total * bins, 0, bins - 1))
        i_cl = int(np.clip((float(row["Close"]) - lo) / span_total * bins, 0, bins - 1))
        span = max(i_hi - i_lo + 1, 1)
        base = v / span
        profile[i_lo:i_hi + 1] += base
        profile[i_cl] += base  # peso extra no preço de fechamento
    return mid, profile


def build_volume_profile_plotly(df: pd.DataFrame, bins: int = 40, price_ref: Optional[float] = None):
    """Retorna (y_levels, volumes, cores) prontos para o bar horizontal do perfil.

    `price_ref` = preço de referência (último close): faixas acima dele em verde,
    abaixo em vermelho — mesma leitura do perfil polarizado do Profit Pro.
    """
    prof = build_volume_profile(df, bins=bins)
    if prof is None:
        return None
    mid, profile = prof
    if price_ref is None:
        price_ref = float(mid[-1])
    colors = ["#26A69A" if m >= price_ref else "#EF5350" for m in mid]
    return list(mid), [float(x) for x in profile], colors


def confirmation_score(df: pd.DataFrame, direction: int,
                       live_q: Optional[dict] = None, max_spread_pips: float = 3.0,
                       pip_size: Optional[float] = None, atr_mult: float = 1.0,
                       max_vol_percentile: float = 90.0):
    """Checklist de confluência para o sinal atual (11 itens).

    `direction`: +1 compra, -1 venda. Cada item é independente:
      1-3 tendência (MM200, MM9×21, ADX) · 4-6 gatilho (pullback EMA50, RSI,
      volatilidade operacional) · 7-8 SMC (sweep a favor, FVG a favor) ·
      9 execução (spread + tick vivo) · 10-11 CLAREZAS (fluxo, absorção/cont).
    Retorna (score, lista de checks) para exibição no painel de decisão.
    """
    checks: list = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    if df is None or df.empty or "Signal" not in df.columns:
        return 0, checks

    last = df.iloc[-1]
    close = float(last["Close"])
    has = lambda col: col in df.columns and pd.notna(last[col])

    # --- 1-3 · Tendência ---------------------------------------------------
    mm200_ok = has("SMA_200") and (close > last["SMA_200"] if direction > 0 else close < last["SMA_200"])
    add("MM200 (viés)", mm200_ok)

    mmx_ok = (has("SMA_9") and has("SMA_21") and
              (last["SMA_9"] > last["SMA_21"] if direction > 0 else last["SMA_9"] < last["SMA_21"]))
    add("MM9×MM21", mmx_ok)

    adx_ok = has("ADX") and last["ADX"] >= 20
    add("ADX ≥ 20", adx_ok, f"{float(last['ADX']):.0f}" if has("ADX") else "")

    # --- 4-5 · Gatilho ------------------------------------------------------
    ema_ok, ema_detail = False, ""
    if has("EMA_50") and has("ATR"):
        atr = float(last["ATR"])
        dist = close - float(last["EMA_50"])
        aligned = dist > 0 if direction > 0 else dist < 0
        near = atr > 0 and abs(dist) <= atr_mult * atr
        ema_ok = aligned and near
        ema_detail = f"{abs(dist) / atr:.2f}×ATR da EMA50" if atr > 0 else ""
    add("Pullback EMA50", ema_ok, ema_detail)

    rsi_ok, rsi_detail = True, ""
    if has("RSI"):
        rsi = float(last["RSI"])
        rsi_ok = (rsi < 70) if direction > 0 else (rsi > 30)
        rsi_detail = f"RSI {rsi:.0f}"
    add("RSI não extremo", rsi_ok, rsi_detail)

    # Volatilidade operacional: percentil na história + chop (ER de Kaufman)
    vol_ok, vol_detail = True, ""
    if has("Vol_Percentile"):
        vp = float(last["Vol_Percentile"])
        vol_ok = vp <= max_vol_percentile
        vol_detail = f"P{vp:.0f}"
        if has("Efficiency_Ratio"):
            er = float(last["Efficiency_Ratio"])
            vol_detail += f" · ER {er:.2f}"
            if er < 0.15:
                vol_ok = False
        if has("Vol_Regime") and str(last["Vol_Regime"]):
            vol_detail += f" ({last['Vol_Regime']})"
    add("Volatilidade operacional", vol_ok, vol_detail)

    # --- 6-7 · SMC -----------------------------------------------------------
    sweeps = find_sweeps(df)
    want = "SSL" if direction > 0 else "BSL"
    sweep_ok = any(e["type"] == want for e in sweeps)
    add("Sweep a favor", sweep_ok, f"{sum(1 for e in sweeps if e['type'] == want)}x")

    bull, bear = get_active_fvgs(df)
    fvg_ok = (len(bull) > 0) if direction > 0 else (len(bear) > 0)
    add("FVG a favor", fvg_ok, f"{len(bull) if direction > 0 else len(bear)} ativo(s)")

    # --- 8 · Execução ---------------------------------------------------------
    spread_ok, sdetail = True, "sem quote ao vivo"
    if live_q and live_q.get("bid"):
        sp = (float(live_q.get("spread") or 0.0) / pip_size) if pip_size else 0.0
        spread_ok = sp <= max_spread_pips if max_spread_pips > 0 else True
        sdetail = f"spread {sp:.1f}p"
    tick_ok = bool(live_q and live_q.get("tick_volume"))
    add("Execução (spread+tick)", spread_ok and tick_ok, sdetail)

    # --- 9-10 · CLAREZAS (Fluxo + Absorção/Continuação) -----------------------
    flow_ok = False
    if has("Clarezas_BuyFlow") and has("Clarezas_SellFlow"):
        if direction > 0:
            flow_ok = bool(last["Clarezas_BuyFlow"])
        else:
            flow_ok = bool(last["Clarezas_SellFlow"])
    add("Fluxo forte (vol+direção)", flow_ok)

    abs_cont_ok = False
    if has("Clarezas_Absorption") and has("Clarezas_ContinuationBuy") and has("Clarezas_ContinuationSell"):
        if direction > 0:
            abs_cont_ok = bool(last["Clarezas_ContinuationBuy"]) or bool(last["Clarezas_Absorption"])
        else:
            abs_cont_ok = bool(last["Clarezas_ContinuationSell"]) or bool(last["Clarezas_Absorption"])
    add("Absorção/Continuação", abs_cont_ok)

    score = sum(1 for c in checks if c["ok"])
    return score, checks


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Recebe OHLCV e devolve com todos os indicadores usados no dashboard."""
    df = df.copy()

    df["SMA_3"] = sma(df["Close"], 3)
    df["SMA_9"] = sma(df["Close"], 9)
    df["SMA_20"] = sma(df["Close"], 20)
    df["SMA_21"] = sma(df["Close"], 21)
    df["SMA_200"] = sma(df["Close"], 200)

    df["EMA_20"] = ema(df["Close"], 20)
    df["EMA_21"] = ema(df["Close"], 21)
    df["EMA_50"] = ema(df["Close"], 50)

    df["RSI"] = rsi(df["Close"], 14)

    macd_line, signal_line, hist = macd(df["Close"])
    df["MACD"] = macd_line
    df["MACD_Signal"] = signal_line
    df["MACD_Hist"] = hist

    df["ATR"] = atr(df["High"], df["Low"], df["Close"], 14)

    bb_upper, bb_mid, bb_lower = bollinger_bands(df["Close"])
    df["BB_Upper"] = bb_upper
    df["BB_Middle"] = bb_mid
    df["BB_Lower"] = bb_lower
    band_width = (bb_upper - bb_lower).replace(0, np.nan)
    df["BB_Position"] = (df["Close"] - bb_mid) / band_width

    df["Volatility"] = df["Close"].rolling(window=5).std()
    df["Volume_MA"] = sma(df["Volume"], 20)

    df["ADX"] = adx(df["High"], df["Low"], df["Close"], 14)

    # Volatilidade precisa: Parkinson/Garman-Klass/Yang-Zhang/realized +
    # percentil de regime + multiplicador de stop adaptativo + ER (chop)
    if _VOL_AVAILABLE:
        df = _add_volatility(df)

    # features auxiliares pro modelo de ML (momentum relativo)
    df["Ret_1"] = df["Close"].pct_change(1)
    df["Ret_3"] = df["Close"].pct_change(3)
    df["Ret_5"] = df["Close"].pct_change(5)
    df["Sma_9_vs_21"] = df["SMA_9"] / df["SMA_21"] - 1
    df["Rsi_vs_50"] = df["RSI"] - 50

    df = find_liquidity_pools(df)
    df = find_swing_liquidity(df)
    df = detect_equal_highs_lows(df)
    df = detect_sweep_candle(df)

    # Clarezas: fluxo + absorção + continuação
    df = clarezas_flow(df)
    df = clarezas_absorption(df)
    df = clarezas_continuation(df)

    # Estado do mercado (equilíbrio vs desequilíbrio)
    df = market_state(df)

    return df


def market_state(df: pd.DataFrame, ema_fast: int = 20, ema_slow: int = 50) -> pd.DataFrame:
    """
    Detecta o estado do mercado:
    - EQUILIBRIO (80%): preço perto das MMs, MMs juntas → mercado lateral
    - DESEQUILIBRIO (20%): preço longe das MMs, MMs abertas → tendência forte
    - TRANSIÇÃO: momento em que o mercado sai do equilíbrio → entrada ideal

    Lógica:
    1. Distância do preço às MMs (%)
    2. Distância entre as MMs (%)
    3. ATR comprimido = volatilidade baixa = prestes a explodir
    4. ADX subindo de <20 para >25 = início de tendência
    """
    out = df.copy()

    # Calcula MMs se não existirem
    if f"EMA_{ema_fast}" not in out.columns:
        out[f"EMA_{ema_fast}"] = out["Close"].ewm(span=ema_fast, adjust=False).mean()
    if f"EMA_{ema_slow}" not in out.columns:
        out[f"EMA_{ema_slow}"] = out["Close"].ewm(span=ema_slow, adjust=False).mean()
    if "ATR" not in out.columns:
        out["ATR"] = atr(out["High"], out["Low"], out["Close"], 14)

    ema_f = out[f"EMA_{ema_fast}"]
    ema_s = out[f"EMA_{ema_slow}"]
    close = out["Close"]

    # 1. Distância do preço à média das MMs (%)
    ma_avg = (ema_f + ema_s) / 2
    dist_price = ((close - ma_avg) / ma_avg * 100).abs()

    # 2. Distância entre as MMs (%)
    dist_ma = ((ema_f - ema_s) / ema_s * 100).abs()

    # 3. ATR normalizado (comparado com média)
    atr_ma = out["ATR"].rolling(50).mean()
    atr_ratio = out["ATR"] / atr_ma

    # 4. Zona do mercado
    out["Market_Zone"] = "EQUILIBRIO"  # padrão

    # DESEQUILIBRIO: preço longe das MMs OU MMs muito separadas
    desequilibrio = (dist_price > 0.5) | (dist_ma > 0.3)
    out.loc[desequilibrio, "Market_Zone"] = "DESEQUILIBRIO"

    # 5. Sinal de TRANSIÇÃO (o mais importante)
    # Saída do equilíbrio: ATR comprimido + ADX subindo + preço saindo da zona
    out["ATR_Compressed"] = atr_ratio < 0.8  # ATR abaixo da média
    out["ADX_Rising"] = out["ADX"] > out["ADX"].shift(1)

    # TRANSIÇÃO: estava em equilíbrio e agora está saindo
    was_equilibrado = dist_price.shift(1) < 0.3
    now_moving = (dist_price > 0.3) | (dist_ma > 0.2)
    out["Transition"] = was_equilibrado & now_moving & out["ATR_Compressed"]

    # 6. Força do desequilíbrio (0 a 1)
    out["Imbalance_Strength"] = (
        (dist_price / dist_price.rolling(50).max().clip(lower=0.01)).clip(0, 1) * 0.5 +
        (dist_ma / dist_ma.rolling(50).max().clip(lower=0.01)).clip(0, 1) * 0.3 +
        (atr_ratio.clip(0, 2) / 2).clip(0, 1) * 0.2
    )

    return out
