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
import re
import threading
from typing import Optional

HOST = "127.0.0.1"
PORT = 18812
CONNECT_TIMEOUT = 5.0

# Magic number do robô. TODA ordem enviada por este sistema carrega este magic,
# e TODA leitura/filtragem de posição usa ele — sem isso o robô conta, altera e
# fecha posições MANUAIS e de outros EAs na mesma conta.
ROBOT_MAGIC = 20260805

# retcode -> tradução (MT5 Trade Server return codes)
RETCODE = {
    10004: "requote (preço mudou)",
    10006: "ordem rejeitada pelo cliente",
    10007: "ordem rejeitada por motivo restrito",
    10008: "ordem antiga (cliente desatualizado)",
    10009: "executada",
    10010: "order modified/partially executed",
    10011: "a mudança não afetou a ordem",
    10012: "preço mudou",
    10013: "preço obsoleto",
    10014: "volume inválido",
    10015: "preço inválido",
    10016: "stops inválidos",
    10017: "swap",
    10018: "mercado fechado",
    10019: "sem saldo/margem suficiente",
    10020: "somente longs",
    10021: "somente shorts",
    10022: "somente compra (proibido vender)",
    10023: "somente venda (proibido comprar)",
    10024: "operação proibida por autoridade",
    10025: "ordens agrupadas proibidas",
    10026: "operações proibidas por Política",
    10027: "demanda de liquidez",
    10028: "conta apenas leitura",
    10029: "ordem agendada",
    10030: "tipo de execução permitido apenas para contas MT5",
    10031: "sem permissão para cancelar/levantar",
    10032: "modo de execução (filling) inválido para o símbolo",
    10033: "sem permissão para comprar (conta hedging)",
    10034: "sem permissão para vender (conta netting)",
    10036: "quantidade de ordens abertas/recentes atingiu o limite",
    10037: "demanda de requisitos de cobertura excedida",
    10038: "conta bloqueada",
    10039: "tempo de vida da ordem expirou",
    10040: "cancelamento indisponível",
    10041: "tipo de pedido inválido",
    10042: "posição bloqueada por ordem oposta",
    10043: "cálculo de margem inválido",
    10044: "volume de risco bloqueado",
}

_lock = threading.Lock()

# Instância compartilhada do processo — evita abrir/derrubar a conexão do
# terminal a cada chamada (causa o MT5 travar com initialize/shutdown a cada refresh).
_shared_bridge = None
_shared_lock = threading.Lock()


def get_bridge() -> "MT5Bridge":
    """Retorna a ponte única do processo (reconecta sozinha se cair)."""
    global _shared_bridge
    if _shared_bridge is None:
        with _shared_lock:
            if _shared_bridge is None:
                _shared_bridge = MT5Bridge()
    return _shared_bridge


class MT5Bridge:
    """Cliente RPyC mínimo para o terminal MetaTrader 5."""

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self._conn: Optional[rpyc.Connection] = None
        # Motivo da última falha de conexão — antes tudo era `except: return None`
        # e o motivo sumia; em operação real isso é o que impede achar o problema.
        self.last_error: str = ""

    # ------------------------------------------------------------- conexão
    def connect(self) -> Optional[rpyc.Connection]:
        # conexão morta (server reiniciou / caiu): derruba e reconecta
        if self._conn is not None and self._conn.closed:
            self._conn = None
        if self._conn is not None:
            return self._conn
        with _lock:
            if self._conn is not None and not self._conn.closed:
                return self._conn
            try:
                conn = rpyc.classic.connect(self.host, self.port)
                conn._config["sync_request_timeout"] = 30
            except Exception as e:
                self.last_error = f"sem conexão com {self.host}:{self.port} ({e})"
                return None
            try:
                conn.execute(
                    "import sys; sys.path.append('C:\\\\mt5libs'); "
                    "import MetaTrader5 as mt5; import datetime"
                )
            except Exception as e:
                self.last_error = f"MetaTrader5 indisponível no servidor ({e})"
                try:
                    conn.close()
                except Exception:
                    pass
                return None
            self._conn = conn
            self.last_error = ""
            return conn

    def available(self) -> bool:
        """True se o servidor RPyC responde (mesmo com o terminal fechado)."""
        try:
            return self.connect() is not None
        except Exception as e:
            self.last_error = str(e)
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
        """Executa expressão no lado do terminal (compatível com eval())."""
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
        try:
            return bool(self._eval("mt5.initialize()"))
        except Exception as e:
            self.last_error = str(e)
            return False

    def ensure_ready(self) -> tuple:
        """
        (ok, motivo) — MUITO mais forte que `initialize()`.

        `mt5.initialize()` devolve True mesmo com o terminal sem sessão de
        trading válida; nesse caso `account_info()` vem None, `equity` vira 0 e
        o sizing antigo mandava 0.01 lote para a conta cega. Aqui exigimos:
        ponte viva + initialize True + conta logada + saldo > 0.
        """
        if self.connect() is None:
            return False, f"bridge RPyC: {self.last_error or 'indisponível'}"
        if not self.initialize():
            err = None
            try:
                err = self.last_error()
            except Exception:
                pass
            return False, f"mt5.initialize() falhou ({err})"
        ai = self.account_info()
        if not ai:
            return False, "conta não logada (account_info() vazio)"
        if not (float(ai.get("equity") or 0) > 0):
            return False, f"equity inválido ({ai.get('equity')}) — sessão de trading inativa"
        return True, ""

    def utc_offset_hours(self) -> float:
        """
        Offset (horário do SERVIDOR − UTC) em horas.

        Medido EMPIRICAMENTE: `mt5.symbol_info_tick().time` vem com o relógio da
        corretora embutido (ex.: 17:45 quando o UTC real é 14:45 → +3h). Como o
        MT5Provider faz `to_datetime(unit='s', utc=True)`, essas horas entram no
        índice dos candles como se fossem UTC — logo o filtro de sessão precisa
        SUBTRAIR este offset para achar a janela real de Londres/NY.

        Retorna 0.0 se não der para detectar (fonte ≠ mt5).
        """
        import time as _time

        cached = getattr(self, "_tz_offset", None)
        if cached is not None and _time.time() - cached[0] < 1800.0:
            return cached[1]

        offset = 0.0
        try:
            import datetime as _dt
            # Varre o Market Watch inteiro numa ÚNICA chamada remota e fica com
            # o tick mais recente. O código antigo pegava symbols_get()[0]
            # (AUDCAD, sem tick) e estourava AttributeError -> 0.0 silencioso,
            # o que deixava a janela de sessão errada em 3 h.
            tick_epoch = int(self._eval(
                "(lambda ns: max([(lambda t: int(t.time) if t is not None else 0)"
                "(mt5.symbol_info_tick(n)) for n in ns] + [0]))"
                "([s.name for s in mt5.symbols_get() if s.visible])"
            ) or 0)
            if tick_epoch <= 0:
                self.last_error = "offset de fuso: nenhum tick no Market Watch"
            else:
                utc_epoch = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
                raw_hours = (tick_epoch - utc_epoch) / 3600.0
                # arredonda em 0.5 h — cobre DST sem inventar fração quebrada
                rounded = round(raw_hours * 2) / 2.0
                if abs(rounded) <= 14.0:
                    # |raw| > 14 h NÃO é fuso: é tick de fim de semana (dias de
                    # diferença). Nesse caso não chuta — sinaliza e fica 0.0.
                    offset = rounded
                else:
                    self.last_error = (f"offset de fuso: tick {raw_hours:+.1f} h de "
                                       f"diferença (mercado fechado) — usando 0.0")
        except Exception as e:
            self.last_error = f"offset de fuso: {e}"
            offset = 0.0

        self._tz_offset = (_time.time(), offset)
        return offset

    def shutdown(self) -> None:
        try:
            self._eval("mt5.shutdown()")
        except Exception:
            pass

    def version(self) -> str:
        return str(self._eval("mt5.version()"))

    def mt5_last_error(self) -> dict:
        """`mt5.last_error()` do terminal.

        O NOME ANTIGO `last_error()` era tapado pelo atributo `self.last_error`
        (str) assim que qualquer falha acontecia → `self.last_error()` virava
        `TypeError: 'str' object is not callable` e o motivo real se perdia.
        """
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
            "margin_free": float(getattr(ai, "margin_free", 0.0) or 0.0),
            "margin_level": float(getattr(ai, "margin_level", 0.0) or 0.0),
            "profit": float(getattr(ai, "profit", 0.0)),
            "leverage": int(getattr(ai, "leverage", 0) or 0),
            "trade_mode": getattr(ai, "trade_mode", None),
            "trade_allowed": bool(getattr(ai, "trade_allowed", True)),
            "trade_expert": bool(getattr(ai, "trade_expert", True)),
        }

    def account_mode(self) -> str:
        """
        'demo', 'contest', 'real' ou 'unknown'.

        `ACCOUNT_TRADE_MODE` (MQL5, na ordem da enumeração):
            0 = ACCOUNT_TRADE_MODE_DEMO
            1 = ACCOUNT_TRADE_MODE_CONTEST
            2 = ACCOUNT_TRADE_MODE_REAL
        O mapeamento antigo estava trocado e classificava a conta LIVE do
        usuário como "contest" — que o gate LIBERAVA, ou seja, entradas em
        dinheiro real sem ALLOW_LIVE_TRADING.

        Fail-safe: qualquer indício de conta real manda o resultado para 'real'
        (nome de servidor live/real, ou enum), e só sai de 'real' se nada
        apontar para live. 'unknown' nunca é tratado como seguro.
        """
        try:
            ai = self._eval("mt5.account_info()")
            if ai is None:
                return "unknown"
            tm = getattr(ai, "trade_mode", None)
            by_enum = {0: "demo", 1: "contest", 2: "real"}.get(tm)
            server = str(getattr(ai, "server", "") or "")
            server_says_live = bool(re.search(r"live|real|\bmt5-?live", server, re.I))
            if by_enum == "real" or server_says_live:
                return "real"
            if by_enum in ("demo", "contest"):
                return by_enum
            return "unknown"
        except Exception as e:
            self.last_error = f"account_mode: {e}"
            return "unknown"

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
            "time": int(getattr(tick, "time", 0) or 0),
            "tick_volume": float(getattr(tick, "volume", 0.0) or 0.0),
            "day_volume": float(getattr(s, "volume", 0.0) or 0.0),
            "point": float(getattr(s, "point", 0.0) or 0.0),
            "digits": int(getattr(s, "digits", 0) or 0),
            "volume_min": float(getattr(s, "volume_min", 0.01) or 0.01),
            "volume_step": float(getattr(s, "volume_step", 0.01) or 0.01),
        }

    def symbol_info(self, symbol: str) -> dict:
        s = self._eval(f"mt5.symbol_info('{symbol}')")
        if s is None:
            return {}
        return {
            "trade_tick_size":  float(getattr(s, "trade_tick_size", 0.0) or 0.0),
            "trade_tick_value": float(getattr(s, "trade_tick_value", 0.0) or 0.0),
            "volume_min":       float(getattr(s, "volume_min", 0.01) or 0.01),
            "volume_max":       float(getattr(s, "volume_max", 100.0) or 100.0),
            "volume_step":      float(getattr(s, "volume_step", 0.01) or 0.01),
            # distância mínima de stops exigida pelo broker (em POINTS) — sem
            # respeitar isso a corretora recusa a ordem com "10016 invalid stops".
            "trade_stops_level": int(getattr(s, "trade_stops_level", 0) or 0),
            "trade_contract_size": float(getattr(s, "trade_contract_size", 100000.0) or 100000.0),
            # bitmask de filling permitido: 1=FOK, 2=IOC, 4=RETURN
            "filling_mode": int(getattr(s, "filling_mode", 0) or 0),
            "trade_mode": int(getattr(s, "trade_mode", 0) or 0),
            "visible": bool(getattr(s, "visible", True)),
            "digits": int(getattr(s, "digits", 0) or 0),
            "point": float(getattr(s, "point", 0.0) or 0.0),
        }

    def filling_code(self, symbol: str) -> str:
        """
        Escolhe o `type_filling` que a corretora ACEITA para o símbolo.

        Antes estava hardcoded ORDER_FILLING_IOC: corretoras que só permitem
        FOK/RETURN rejeitam a ordem inteira com 10032 e o robô só via "recusou".
        """
        mode = int(self.symbol_info(symbol).get("filling_mode", 0) or 0)
        if mode & 2:
            return "mt5.ORDER_FILLING_IOC"
        if mode & 4:
            return "mt5.ORDER_FILLING_RETURN"
        if mode & 1:
            return "mt5.ORDER_FILLING_FOK"
        return "mt5.ORDER_FILLING_IOC"  # default histórico do projeto

    def history_deals_today(self, magic: int = ROBOT_MAGIC) -> Optional[list]:
        """
        Deals fechados no dia corrente do servidor.

        Padrão = só deals do robô (`magic=None` devolve todos).
        IMPORTANTE: devolve `None` quando a consulta FALHA (vs `[]` quando
        realmente não há deals). O risk_guard de perda diária converte erro em
        "pnl = 0" e LIBERAVA a negociação — a guarda falhava exatamente quando
        precisava existir. Com None, o chamador bloqueia (fail-safe).
        """
        conn = self.connect()
        if not conn:
            return None
        _m = None if magic is None else int(magic)
        code = (
            "[{'profit': float(d.profit), 'magic': int(getattr(d, 'magic', 0))} for d in "
            "(mt5.history_deals_get("
            "datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0), "
            "datetime.datetime.now() + datetime.timedelta(days=1)) or [])]"
        )
        try:
            result = self._eval(code)
            deals = list(result) if result is not None else []
            if _m is None:
                return deals
            # só deals do robô: misturar trades manuais distorce o limite diário
            return [d for d in deals if int(d.get("magic") or 0) == _m]
        except Exception as e:
            self.last_error = f"history_deals_today: {e}"
            return None

    def history_deals_get(self, days: int = 30, magic: int = ROBOT_MAGIC) -> Optional[list]:
        conn = self.connect()
        if not conn:
            return None
        _m = None if magic is None else int(magic)
        code = (
            "[{"
            "'ticket': int(getattr(d, 'ticket', 0)), "
            "'symbol': str(getattr(d, 'symbol', '')), "
            "'type': int(getattr(d, 'type', 0)), "
            "'entry': int(getattr(d, 'entry', 0)), "
            "'volume': float(getattr(d, 'volume', 0.0)), "
            "'price': float(getattr(d, 'price', 0.0)), "
            "'profit': float(getattr(d, 'profit', 0.0)), "
            "'time': int(getattr(d, 'time', 0)), "
            "'magic': int(getattr(d, 'magic', 0)), "
            "'comment': str(getattr(d, 'comment', '')) "
            "} for d in "
            "(mt5.history_deals_get("
            "datetime.datetime.now() - datetime.timedelta(days=" + str(days) + "), "
            "datetime.datetime.now() + datetime.timedelta(days=1)) or [])]"
        )
        try:
            result = self._eval(code)
            deals = list(result) if result is not None else []
            if _m is None:
                return deals
            return [d for d in deals if int(d.get("magic") or 0) == _m]
        except Exception as e:
            self.last_error = f"history_deals_get: {e}"
            return None

    def debug_deals(self, days: int = 30) -> dict:
        """Método de diagnóstico para verificar deals disponíveis."""
        deals = self.history_deals_get(days)
        if deals is None:
            return {"total": 0, "deals": [], "error": self.last_error}
        return {"total": len(deals), "deals": deals}

    # -------------------------------------------------------------- orders
    def _market_request(self, symbol: str, side: int, volume: float, price: float,
                        sl: Optional[float], tp: Optional[float], digits: int,
                        magic: int, comment: str, position: Optional[int] = None) -> str:
        """Monta o request de ordem a mercado como código Python do lado remoto."""
        order_type = "mt5.ORDER_TYPE_BUY" if side == 1 else "mt5.ORDER_TYPE_SELL"
        filling = self.filling_code(symbol)
        _r = lambda v: round(float(v), digits) if v else 0.0
        pos_field = f"'position': {int(position)}, " if position else ""
        return (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_DEAL, "
            f"{pos_field}"
            f"'symbol': '{symbol}', "
            f"'volume': {float(volume):.2f}, "
            f"'type': {order_type}, "
            f"'price': {_r(price)}, "
            f"'sl': {_r(sl)}, "
            f"'tp': {_r(tp)}, "
            f"'deviation': 10, "
            f"'magic': {int(magic)}, "
            f"'comment': '{comment}', "
            f"'type_time': mt5.ORDER_TIME_GTC, "
            f"'type_filling': {filling} "
            f"}})"
        )

    def order_check(self, request_code: str) -> dict:
        """
        Simula a ordem no servidor SEM enviar (retcode + margem exigida).
        Rascunho: se `margin_required > margin_free`, a ordem seria rejeitada
        por 10019 — melhor descobrir aqui do que depois de um fill parcial.
        """
        try:
            r = self._eval(f"mt5.order_check({request_code})")
        except Exception as e:
            return {"retcode": -1, "comment": f"order_check falhou: {e}"}
        if r is None:
            return {"retcode": -1, "comment": "order_check retornou None"}
        return {
            "retcode": int(getattr(r, "retcode", -1) or -1),
            "comment": str(getattr(r, "comment", "")),
            "margin": float(getattr(r, "margin", 0.0) or 0.0),
            "margin_required": float(getattr(r, "margin_required", 0.0) or 0.0),
            "volume": float(getattr(r, "volume", 0.0) or 0.0),
        }

    def place_market(self, symbol: str, side: int, volume: float,
                     sl: Optional[float] = None, tp: Optional[float] = None,
                     magic: int = ROBOT_MAGIC, comment: str = "forex_dashboard",
                     deviation: int = 10) -> dict:
        """
        Envia ordem a mercado. side: +1 compra, -1 venda.

        Retorna dict normalizado: {retcode, comment, code_text, order, volume, price}.
        `code_text` traduz o retcode do MT5 — antes só aparecia o número/caption
        e era impossível saber se foi 10019 (sem margem) ou 10032 (filling).
        """
        q = self.quote(symbol)
        if not q or not q.get("ask"):
            return {"retcode": -1, "comment": f"sem cotação para {symbol}",
                    "code_text": "sem cotação"}
        digits = int(q.get("digits") or 5)
        price = q["ask"] if side == 1 else q["bid"]

        info = self.symbol_info(symbol)
        # stops_level = distância mínima do broker em POINTS
        stops_level = int(info.get("trade_stops_level", 0) or 0)
        point = float(q.get("point") or info.get("point") or 0.0) or 1e-5
        if stops_level and sl:
            min_dist = stops_level * point
            if abs(price - float(sl)) < min_dist:
                sl = price - min_dist if price > float(sl) else price + min_dist
        if stops_level and tp:
            min_dist = stops_level * point
            if abs(price - float(tp)) < min_dist:
                tp = price - min_dist if price > float(tp) else price + min_dist

        code = self._market_request(symbol, side, volume, price, sl, tp,
                                    digits, magic, comment)
        # 1) checa antes de enviar (margem / stopping level / filling).
        #    retcode -1 = não conseguiu checar → segue e deixa o envio decidir.
        chk = self.order_check(code)
        chk_code = chk.get("retcode")
        if chk_code not in (-1, 10009, 10004, None):
            return {
                "retcode": chk_code,
                "comment": chk.get("comment", ""),
                "code_text": RETCODE.get(chk_code, str(chk_code)),
                "margin_required": chk.get("margin_required", 0.0),
                "rejected_by": "order_check",
            }

        # 2) envia; requote (10004) => refaz 1× com o preço atual
        res = self._eval(code)
        retcode = int(getattr(res, "retcode", -1) or -1) if res is not None else -1
        if retcode == 10004:
            q2 = self.quote(symbol)
            if q2 and q2.get("ask"):
                price = q2["ask"] if side == 1 else q2["bid"]
                code = self._market_request(symbol, side, volume, price, sl, tp,
                                            digits, magic, comment)
                res = self._eval(code)
                retcode = int(getattr(res, "retcode", -1) or -1) if res is not None else -1
        if res is None:
            return {"retcode": -1, "comment": "order_send retornou None",
                    "code_text": "sem resposta do servidor"}
        return {
            "retcode": retcode,
            "comment": str(getattr(res, "comment", "")),
            "code_text": RETCODE.get(retcode, str(retcode)),
            "order": int(getattr(res, "order", 0) or 0),
            "volume": float(getattr(res, "volume", 0.0) or 0.0),
            "price": float(getattr(res, "price", 0.0) or 0.0),
        }

    def modify_position_stops(self, ticket: int, symbol: str,
                              sl: float, tp: float) -> dict:
        """
        MOVE o SL/TP de uma posição já aberta (TRADE_ACTION_SLTP).

        Isto é o que implementa o TRAILING STOP de verdade — antes o parâmetro
        `trailing_stop_pips` era aceito pela interface e ignorado: não existia
        nenhum `TRADE_ACTION_SLTP`/`position_modify` em todo o repositório.
        """
        q = self.quote(symbol)
        digits = int(q.get("digits") or 5)
        code = (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_SLTP, "
            f"'position': {int(ticket)}, "
            f"'symbol': '{symbol}', "
            f"'sl': {round(float(sl), digits) if sl else 0.0}, "
            f"'tp': {round(float(tp), digits) if tp else 0.0} "
            f"}})"
        )
        res = self._eval(code)
        if res is None:
            return {"retcode": -1, "comment": "sem resposta", "code_text": "sem resposta"}
        retcode = int(getattr(res, "retcode", -1) or -1)
        return {"retcode": retcode,
                "comment": str(getattr(res, "comment", "")),
                "code_text": RETCODE.get(retcode, str(retcode))}

    def place_pending(self, symbol: str, order_type: str, volume: float,
                      price: float, sl: Optional[float] = None, tp: Optional[float] = None,
                      magic: int = ROBOT_MAGIC, comment: str = "forex_dashboard") -> dict:
        """
        Ordem pendente no MT5.
        order_type: BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP
        """
        q = self.quote(symbol)
        digits = q.get("digits") or 5
        _r = lambda v: round(float(v), digits) if v else 0.0
        filling = self.filling_code(symbol)
        code = (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_PENDING, "
            f"'symbol': '{symbol}', "
            f"'volume': {float(volume):.2f}, "
            f"'type': mt5.ORDER_TYPE_{order_type.upper()}, "
            f"'price': {_r(price)}, "
            f"'sl': {_r(sl)}, "
            f"'tp': {_r(tp)}, "
            f"'deviation': 10, "
            f"'magic': {int(magic)}, "
            f"'comment': '{comment}', "
            f"'type_time': mt5.ORDER_TIME_GTC, "
            f"'type_filling': {filling} "
            f"}})"
        )
        res = self._eval(code)
        if res is None:
            return {"retcode": -1, "comment": "sem resposta", "code_text": "sem resposta"}
        retcode = int(getattr(res, "retcode", -1) or -1)
        return {"retcode": retcode,
                "comment": str(getattr(res, "comment", "")),
                "code_text": RETCODE.get(retcode, str(retcode))}

    def pending_orders(self) -> list:
        """Lista ordens pendentes ativas."""
        orders = self._eval("mt5.orders_get()")
        if not orders:
            return []
        out = []
        for o in orders:
            out.append({
                "ticket": int(o.ticket),
                "symbol": o.symbol,
                "type": int(o.type),
                "type_name": str(o.type).split(".")[-1] if hasattr(o, 'type') else "",
                "volume": float(o.volume_open),
                "price": float(o.price_open),
                "sl": float(o.sl),
                "tp": float(o.tp),
                "magic": int(o.magic),
                "comment": str(o.comment),
                "time_setup": int(o.time_setup),
            })
        return out

    def cancel_order(self, ticket: int) -> dict:
        """Cancela uma ordem pendente pelo ticket."""
        code = (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_REMOVE, "
            f"'order': {ticket} "
            f"}})"
        )
        res = self._eval(code)
        if res is None:
            return {"retcode": -1, "comment": "sem resposta", "code_text": "sem resposta"}
        retcode = int(getattr(res, "retcode", -1) or -1)
        return {"retcode": retcode,
                "comment": str(getattr(res, "comment", "")),
                "code_text": RETCODE.get(retcode, str(retcode))}

    def close_symbol(self, symbol: str, magic: Optional[int] = None) -> list:
        """
        Fecha as posições do símbolo. COM `magic` (padrão: o do robô) — sem
        filtro o "Zerar conta" fechava posições manuais e de outros EAs.
        """
        results = []
        for pos in self.positions(magic=magic):
            if pos["symbol"] != symbol:
                continue
            results.append(self.close_ticket(pos["ticket"]))
        return results

    def close_ticket(self, ticket: int) -> dict:
        """Fecha uma posição específica pelo ticket, com preço arredondado p/ digits."""
        pos = None
        for p in self.positions(magic=None):
            if p["ticket"] == ticket:
                pos = p
                break
        if pos is None:
            return {"retcode": -1, "comment": "posição não encontrada",
                    "code_text": "posição não encontrada"}
        symbol = pos["symbol"]
        close_type = "mt5.ORDER_TYPE_SELL" if pos["side"] == 1 else "mt5.ORDER_TYPE_BUY"
        q = self.quote(symbol)
        if not q or not q.get("bid"):
            return {"retcode": -1, "comment": f"sem cotação de {symbol}",
                    "code_text": "sem cotação"}
        digits = int(q.get("digits") or 5)
        price = q["bid"] if pos["side"] == 1 else q["ask"]
        filling = self.filling_code(symbol)
        code = (
            f"mt5.order_send({{"
            f"'action': mt5.TRADE_ACTION_DEAL, "
            f"'position': {int(ticket)}, "
            f"'symbol': '{symbol}', "
            f"'volume': {pos['volume']:.2f}, "
            f"'type': {close_type}, "
            f"'price': {round(float(price), digits)}, "
            f"'deviation': 10, "
            f"'magic': {int(ROBOT_MAGIC)}, "
            f"'comment': 'forex_dashboard_close', "
            f"'type_time': mt5.ORDER_TIME_GTC, "
            f"'type_filling': {filling} "
            f"}})"
        )
        res = self._eval(code)
        if res is None:
            return {"retcode": -1, "comment": "sem resposta", "code_text": "sem resposta",
                    "price": price}
        retcode = int(getattr(res, "retcode", -1) or -1)
        return {"retcode": retcode,
                "comment": str(getattr(res, "comment", "")),
                "code_text": RETCODE.get(retcode, str(retcode)),
                "order": int(getattr(res, "order", 0) or 0),
                "price": price}

    def positions(self, magic: int = ROBOT_MAGIC) -> list:
        """
        Posições abertas.

        PADRÃO = só as do ROBOT_MAGIC. Todos os chamadores (executor, watchdog)
        esperam isso: sem o filtro o robô contava, alterava e FECHAVA posições
        manuais e de outros EAs. Passe explicitamente `magic=None` para ver
        todas (diagnóstico).
        """
        try:
            pos = self._eval("mt5.positions_get()")
        except Exception as e:
            self.last_error = f"positions_get: {e}"
            return []
        if not pos:
            return []
        out = []
        for p in pos:
            m = int(getattr(p, "magic", 0) or 0)
            if magic is not None and m != int(magic):
                continue
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
                "magic": m,
                "comment": str(getattr(p, "comment", "")),
            })
        return out
