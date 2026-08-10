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

    return df
