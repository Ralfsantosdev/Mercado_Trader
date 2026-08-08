"""
Camada de dados do dashboard.

Por que existe essa abstração: yfinance é usado hoje como fonte, mas não é
confiável para forex intradiário curto (M5) de forma gratuita. Quando você
tiver um CSV do Dukascopy/MetaTrader ou uma API paga (Twelve Data, Alpha
Vantage), basta criar uma nova classe herdando de DataProvider e trocar
uma linha no app.py — nada mais muda.
"""

import json
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import streamlit as st


class DataProvider(ABC):
    """Interface que qualquer fonte de dados precisa implementar."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        """
        Deve retornar um DataFrame com colunas:
        Open, High, Low, Close, Volume — index datetime, ordenado do mais
        antigo para o mais recente.
        """
        raise NotImplementedError


class YFinanceProvider(DataProvider):
    """Fonte atual: Yahoo Finance via yfinance. Timeframe mínimo confiável: 1h."""

    @st.cache_data(ttl=3600, show_spinner=False)
    def _fetch(_self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=days)
        df = yf.download(
            symbol, start=start, end=end, interval=interval,
            progress=False, auto_adjust=False, threads=False,
        )

        if df.empty:
            return df

        # yfinance às vezes devolve colunas multi-index (symbol, campo) — normaliza
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.dropna(inplace=True)
        return df

    def get_ohlcv(self, symbol: str, days: int, interval: str = "1h") -> pd.DataFrame:
        return self._fetch(symbol, days, interval)


class CSVProvider(DataProvider):
    """
    Fonte para quando você tiver histórico exportado (Dukascopy, MetaTrader).
    Aceita colunas: datetime/time/date, open, high, low, close, volume (case-insensitive).
    Volume é opcional (preenchido com 0 se ausente).
    """

    def __init__(self, filepath: str):
        self.filepath = filepath

    def get_ohlcv(self, symbol: str, days: int, interval: str = "5min") -> pd.DataFrame:
        df = pd.read_csv(self.filepath)
        df.columns = [str(c).strip().lower() for c in df.columns]

        date_col = next((c for c in df.columns if c in ("datetime", "time", "date", "timestamp")), None)
        if date_col is None:
            raise ValueError("CSV sem coluna de data/hora (datetime, time, date ou timestamp).")
        df = df.rename(columns={date_col: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)

        required = ["open", "high", "low", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"CSV sem colunas obrigatórias: {missing}")

        df = df.rename(columns={c: c.capitalize() for c in required})
        if "volume" not in df.columns:
            df["Volume"] = 0
        else:
            df = df.rename(columns={"volume": "Volume"})

        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        if days:
            cutoff = df.index.max() - timedelta(days=days)
            df = df[df.index >= cutoff]

        return df[["Open", "High", "Low", "Close", "Volume"]]


class AwesomeAPIProvider(DataProvider):
    """
    Fonte: AwesomeAPI (economia.awesomeapi.com.br) — cotação e análise em tempo real.

    Endpoints usados:
    - /json/last/{moedas}  → última cotação (tempo real, sem cache com API key)
    - /{moeda}/{n}         → cotações sequenciais (intervalo ~1 min), máx 1500
    - /json/daily/{moeda}/{dias} → fechamento diário (até 360 dias)

    Limitações conhecidas:
    - Volume não existe no AwesomeAPI → preenchido com contagem de ticks/candles.
    - Intraday só tem ~1 ponto por minuto; o histórico sequencial com paginação
      por end_date cobre os últimos ~30-60 dias dependendo do par.
    """

    BASE_URL = "https://economia.awesomeapi.com.br"
    MAX_POINTS = 1500  # limite de pontos por chamada com API key

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _normalize(symbol: str) -> str:
        """'EURUSD=X' → 'EUR-USD' (formato do AwesomeAPI)."""
        s = symbol.replace("=X", "").upper()
        if "-" in s:
            return s
        if len(s) == 6:
            return f"{s[:3]}-{s[3:]}"
        return s

    def _get_json(self, path: str, retries: int = 3) -> object:
        url = f"{self.BASE_URL}/{path}"
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        last_err = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:  # timeout / 5xx / rede — tenta de novo
                last_err = e
        raise RuntimeError(f"Falha ao consultar AwesomeAPI ({url}): {last_err}")

    @staticmethod
    def _ts_to_dt(ts: int) -> pd.Timestamp:
        # timestamp às vezes vem em milissegundos (13 dígitos) ou segundos (10)
        if ts > 10**12:
            ts = ts // 1000
        return pd.Timestamp(ts, unit="s", tz="UTC")

    # ------------------------------------------------------------ público
    def get_last_quote(self, symbol: str) -> dict:
        """Última cotação em tempo real (bid/ask/high/low)."""
        data = self._get_json(f"json/last/{self._normalize(symbol)}")
        if not isinstance(data, dict):
            raise ValueError(f"Resposta inesperada do AwesomeAPI: {data}")
        return list(data.values())[0]

    def get_ohlcv(self, symbol: str, days: int, interval: str = "5min") -> pd.DataFrame:
        pair = self._normalize(symbol)
        if interval == "1d":
            return self._fetch_daily(pair, days)
        return self._fetch_intraday(pair, days, interval)

    # -------------------------------------------------------- implementação
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_daily(_self, pair: str, days: int) -> pd.DataFrame:
        n = max(1, min(days, 360))
        data = _self._get_json(f"json/daily/{pair}/{n}")
        if not isinstance(data, list) or not data:
            return pd.DataFrame()

        rows = []
        for item in data:
            dt = _self._ts_to_dt(int(item["timestamp"]))
            bid = float(item.get("bid", 0.0))
            ask = float(item.get("ask", bid))
            rows.append({
                "datetime": dt,
                "open": bid,
                "high": float(item.get("high", bid)),
                "low": float(item.get("low", bid)),
                "close": bid,
                "volume": 0,
            })
        df = pd.DataFrame(rows).set_index("datetime")
        df.columns = [c.capitalize() for c in df.columns]
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]
        return df[["Open", "High", "Low", "Close", "Volume"]]

    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_intraday(_self, pair: str, days: int, interval: str) -> pd.DataFrame:
        points = _self._paginate_sequential(pair, days)
        if not points:
            return pd.DataFrame()

        rows = []
        for p in points:
            dt = _self._ts_to_dt(int(p["timestamp"]))
            bid = float(p.get("bid", 0.0))
            ask = float(p.get("ask", bid))
            rows.append({"datetime": dt, "price": (bid + ask) / 2, "vol": 1})
        df = pd.DataFrame(rows)
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        freq_map = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}
        freq = freq_map.get(interval, "5min")
        ohlc = df["price"].resample(freq).agg(["first", "max", "min", "last"])
        volume = df["vol"].resample(freq).sum()
        ohlc["volume"] = volume
        ohlc.columns = ["Open", "High", "Low", "Close", "Volume"]
        ohlc.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

        if days:
            cutoff = ohlc.index.max() - timedelta(days=days)
            ohlc = ohlc[ohlc.index >= cutoff]

        return ohlc

    def _paginate_sequential(self, pair: str, days: int) -> list:
        """
        Junta janelas de ~1 dia até cobrir `days` dias de histórico (~1 min/tick).

        A API devolve, para cada janela [start_date, end_date], apenas os últimos
        ~1500 pontos (≈ 1 dia) — então para recuar no tempo o end_date precisa
        andar para trás um dia inteiro por vez, senão fica no mesmo dia e
        retorna sempre os mesmos pontos.
        """
        points = []
        end = datetime.now()
        target = end - timedelta(days=days)
        guard = 0
        while True:
            start = end - timedelta(days=days + 2)
            sd = start.strftime("%Y%m%d")
            ed = end.strftime("%Y%m%d")
            path = f"{pair}/{self.MAX_POINTS}?start_date={sd}&end_date={ed}"
            try:
                data = self._get_json(path)
            except Exception:
                # janela falhou (timeout/parcial) — interrompe com o que já temos
                break
            if not isinstance(data, list) or not data:
                break
            points.extend(data)
            oldest = min(int(p["timestamp"]) for p in data)
            oldest_dt = datetime.fromtimestamp(oldest)
            if oldest_dt < target:
                break
            # recua para o dia anterior ao tick mais antigo devolvido
            end = datetime(oldest_dt.year, oldest_dt.month, oldest_dt.day) - timedelta(minutes=1)
            guard += 1
            if guard > days + 3:
                break
        # o feed devolve do mais recente pro mais antigo em janelas — deduplica por ts
        uniq = {}
        for p in points:
            uniq[int(p["timestamp"])] = p
        return list(uniq.values())


def get_provider(source: str = "yfinance", csv_path: Optional[str] = None, api_key: Optional[str] = None) -> DataProvider:
    """Fábrica simples — troque a fonte padrão aqui quando migrar."""
    if source == "awesomeapi":
        return AwesomeAPIProvider(api_key=api_key)
    elif source == "yfinance":
        return YFinanceProvider()
    elif source == "csv":
        if not csv_path:
            raise ValueError("csv_path é obrigatório quando source='csv'")
        return CSVProvider(csv_path)
    else:
        raise ValueError(f"Fonte desconhecida: {source}")
