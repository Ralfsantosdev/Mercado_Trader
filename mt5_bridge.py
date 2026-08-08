"""
Bridge leve para o MetaTrader 5 rodando sob Wine, via servidor RPyC.

O `mt5server.exe` (do projeto mt5linux) expõe o terminal MT5 como um servidor
RPyC clássico na porta 18812. Este módulo é um cliente mínimo que fala
diretamente com esse servidor, sem depender do pacote `mt5linux` (que exige
Python 3.12 — este projeto roda em 3.8).

Para o bridge funcionar, o MT5 precisa estar ABERTO e LOGADO, e o servidor
rodando (ver `start_mt5.sh` / README):

    wine mt5server.exe     # no mesmo prefix do MT5, porta 18812

Uso:
    from mt5_bridge import MT5Bridge
    mt5 = MT5Bridge()
    if mt5.available():
        mt5.initialize()
        acc = mt5.account_info()          # dict com login/server/balance...
        bid, ask = mt5.quote("EURUSD")
        mt5.shutdown()
"""

import rpyc
import threading
from typing import Optional

HOST = "127.0.0.1"
PORT = 18812
CONNECT_TIMEOUT = 5.0

_lock = threading.Lock()


class MT5Bridge:
    """Cliente RPyC mínimo para o terminal MetaTrader 5."""

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self._conn: Optional[rpyc.Connection] = None

    # ------------------------------------------------------------- conexão
    def connect(self) -> Optional[rpyc.Connection]:
        if self._conn is not None:
            return self._conn
        with _lock:
            if self._conn is not None:
                return self._conn
            try:
                conn = rpyc.classic.connect(self.host, self.port)
                conn._config["sync_request_timeout"] = 30
            except Exception:
                return None
            try:
                conn.execute(
                    "import sys; sys.path.append('C:\\\\mt5libs'); "
                    "import MetaTrader5 as mt5; import datetime"
                )
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                return None
            self._conn = conn
            return conn

    def available(self) -> bool:
        """True se o servidor RPyC responde (mesmo com o terminal fechado)."""
        try:
            return self.connect() is not None
        except Exception:
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # -------------------------------------------------------------- helpers
    def _eval(self, code: str):
        """Executa código no lado do terminal.

        Retorna a referência remota (netref). Valores simples (int/str/float)
        são transparentes; structs MT5 (AccountInfo, posições etc.) não são
        pickláveis, então o acesso é sempre por atributo — nunca por obtain.
        """
        conn = self.connect()
        if conn is None:
            raise ConnectionError("Servidor MT5 (mt5server.exe) não está rodando.")
        return conn.eval(code)

    def _attr(self, name: str):
        """Acessa um atributo remoto (evita pickling de objetos MT5)."""
        conn = self.connect()
        if conn is None:
            raise ConnectionError("Servidor MT5 (mt5server.exe) não está rodando.")
        return getattr(conn.namespace["mt5"], name)

    # ------------------------------------------------------------- API MT5
    def initialize(self) -> bool:
        return bool(self._eval("mt5.initialize()"))

    def shutdown(self) -> None:
        try:
            self._eval("mt5.shutdown()")
        except Exception:
            pass

    def version(self) -> str:
        return str(self._eval("mt5.version()"))

    def last_error(self) -> dict:
        return self._eval("mt5.last_error()")

    # ------------------------------------------------------------ accounts
    def account_info(self) -> dict:
        ai = self._eval("mt5.account_info()")
        if ai is None:
            return {}
        return {
            "login": getattr(ai, "login", None),
            "server": getattr(ai, "server", None),
            "company": getattr(ai, "company", None),
            "name": getattr(ai, "name", None),
            "currency": getattr(ai, "currency", None),
            "balance": float(getattr(ai, "balance", 0.0)),
            "equity": float(getattr(ai, "equity", 0.0)),
            "margin": float(getattr(ai, "margin", 0.0)),
            "profit": float(getattr(ai, "profit", 0.0)),
            "trade_mode": getattr(ai, "trade_mode", None),
        }

    # -------------------------------------------------------------- quotes
    def quote(self, symbol: str) -> dict:
        s = self._eval(f"mt5.symbol_info('{symbol}')")
        if s is None:
            return {}
        tick = self._eval(f"mt5.symbol_info_tick('{symbol}')")
        # símbolos fora do Market Watch (ex.: XAUUSD) não streamam bid/ask —
        # adiciona ao Market Watch e tenta de novo
        if tick is None or (getattr(tick, "bid", 0.0) or 0.0) == 0.0:
            try:
                self._eval(f"mt5.symbol_select('{symbol}', True)")
            except Exception:
                pass
            tick = self._eval(f"mt5.symbol_info_tick('{symbol}')")
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        return {
            "symbol": getattr(s, "name", symbol),
            "bid": bid,
            "ask": ask,
            "spread": max(ask - bid, 0.0),
            "last": float(getattr(tick, "last", 0.0) or 0.0),
            "tick_volume": float(getattr(tick, "volume", 0.0) or 0.0),
            "day_volume": float(getattr(s, "trade_volume", 0.0) or 0.0),
            "point": float(getattr(s, "point", 0.0) or 0.0),
            "digits": int(getattr(s, "digits", 0) or 0),
            "volume_min": float(getattr(s, "volume_min", 0.01) or 0.01),
            "volume_step": float(getattr(s, "volume_step", 0.01) or 0.01),
        }

    # -------------------------------------------------------------- orders
    def place_market(self, symbol: str, side: int, volume: float,
                     sl: Optional[float] = None, tp: Optional[float] = None,
                     magic: int = 20260805, comment: str = "forex_dashboard") -> dict:
        """side: +1 compra, -1 venda. Retorna dict com retcode/comment."""
        order_type = "mt5.ORDER_TYPE_BUY" if side == 1 else "mt5.ORDER_TYPE_SELL"
        q = self.quote(symbol)
        price = q["ask"] if side == 1 else q["bid"]
        digits = q["digits"] or 5
        _r = lambda v: round(float(v), digits)
        code = (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_DEAL, "
            f"'symbol': '{symbol}', "
            f"'volume': {float(volume):.2f}, "
            f"'type': {order_type}, "
            f"'price': {_r(price)}, "
            f"'sl': {_r(sl) if sl else 0.0}, "
            f"'tp': {_r(tp) if tp else 0.0}, "
            f"'deviation': 10, "
            f"'magic': {magic}, "
            f"'comment': '{comment}', "
            f"'type_time': mt5.ORDER_TIME_GTC, "
            f"'type_filling': mt5.ORDER_FILLING_IOC "
            f"}})"
        )
        return self._eval(code)

    def close_symbol(self, symbol: str) -> list:
        """Fecha todas as posições do símbolo. Retorna lista de resultados."""
        results = []
        for pos in self.positions():
            if pos["symbol"] != symbol:
                continue
            close_type = "mt5.ORDER_TYPE_SELL" if pos["side"] == 1 else "mt5.ORDER_TYPE_BUY"
            q = self.quote(symbol)
            price = q["bid"] if pos["side"] == 1 else q["ask"]
            code = (
                f"mt5.order_send({{"
                f"'action': mt5.TRADE_ACTION_DEAL, "
                f"'position': {pos['ticket']}, "
                f"'symbol': '{symbol}', "
                f"'volume': {pos['volume']:.2f}, "
                f"'type': {close_type}, "
                f"'price': {price}, "
                f"'deviation': 10, "
                f"'magic': 20260805, "
                f"'comment': 'forex_dashboard_close', "
                f"'type_time': mt5.ORDER_TIME_GTC, "
                f"'type_filling': mt5.ORDER_FILLING_IOC "
                f"}})"
            )
            results.append(self._eval(code))
        return results

    def positions(self) -> list:
        pos = self._eval("mt5.positions_get()")
        if not pos:
            return []
        out = []
        for p in pos:
            out.append({
                "ticket": int(p.ticket),
                "symbol": p.symbol,
                "side": 1 if p.type == 0 else -1,
                "volume": float(p.volume),
                "entry": float(p.price_open),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "price_current": float(p.price_current),
            })
        return out
