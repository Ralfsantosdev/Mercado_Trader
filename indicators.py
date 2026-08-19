"""
Indicadores técnicos, implementados sem dependência externa (pandas_ta),
para evitar problema de instalação e para eu poder garantir a fórmula exata
que você está acostumado a ver na IQ Option / MetaTrader.
"""

import pandas as pd
import numpy as np


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
    """
    recent = df.iloc[-max_lookback:] if len(df) > max_lookback else df
    
    bull_gaps = []
    bear_gaps = []
    
    for idx, row in recent.iterrows():
        # Se achou um gap, adiciona na lista
        if pd.notna(row.get("FVG_Bull_Top")):
            bull_gaps.append({"top": row["FVG_Bull_Top"], "bot": row["FVG_Bull_Bot"], "time": idx})
        if pd.notna(row.get("FVG_Bear_Top")):
            bear_gaps.append({"top": row["FVG_Bear_Top"], "bot": row["FVG_Bear_Bot"], "time": idx})
            
        # Remove gaps que foram mitigados (preenchidos pelo preço)
        # Um gap de compra continua ativo enquanto o preço não cair abaixo do seu fundo
        bull_gaps = [g for g in bull_gaps if row["Low"] > g["bot"]]
        # Um gap de venda continua ativo enquanto o preço não subir acima do seu topo
        bear_gaps = [g for g in bear_gaps if row["High"] < g["top"]]
        
    return bull_gaps, bear_gaps

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


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Recebe OHLCV e devolve com todos os indicadores usados no dashboard."""
    df = df.copy()

    df["SMA_3"] = sma(df["Close"], 3)
    df["SMA_9"] = sma(df["Close"], 9)
    df["SMA_20"] = sma(df["Close"], 20)
    df["SMA_21"] = sma(df["Close"], 21)
    df["SMA_200"] = sma(df["Close"], 200)

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

    # features auxiliares pro modelo de ML (momentum relativo)
    df["Ret_1"] = df["Close"].pct_change(1)
    df["Ret_3"] = df["Close"].pct_change(3)
    df["Ret_5"] = df["Close"].pct_change(5)
    df["Sma_9_vs_21"] = df["SMA_9"] / df["SMA_21"] - 1
    df["Rsi_vs_50"] = df["RSI"] - 50

    df = find_liquidity_pools(df)
    df = find_swing_liquidity(df)

    return df
