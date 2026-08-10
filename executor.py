"""
Paper trading / execução ao vivo (modo demo).

Diferente do backtest (que roda sobre um DataFrame fechado), este módulo
acompanha a cotação EM TEMPO REAL da AwesomeAPI e executa ordens como uma
corretora faria:
- Abre posição quando o sinal muda para compra/venda, no preço bid/ask atual.
- Aplica um slippage configurável (pips) — simula a degradação real de execução.
- Mantém SL/TP por ATR e risco % por trade (mesma lógica do backtest).
- Persiste posições e histórico em JSON local — sobrevive a reinícios do app.

Estrutura pronta para plugar corretora real: a interface é uma classe `Broker`
com os métodos abstratos; `PaperBroker` é a implementação simulada. Quando você
tiver acesso a uma corretora demo de verdade (MetaTrader5, OANDA practice etc.),
basta criar `LiveBroker(Broker)` com a mesma interface e trocar UMA linha.

AVISO: papel é papel. NÃO garante que a corretora real executará igual —
slippage real, requotes e downtime só aparecem na execução de verdade.
"""

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execution_state.json")

PIP_SIZE = 0.0001  # maioria dos pares; JPY usa 0.01 (ajustar se operar JPY)

CONTRACT_SIZE = 100_000  # 1 lote padrão = 100k unidades da base (forex)

# Limites de segurança anti-implosão de posição (foram a causa do trade de -225)
MIN_SL_PIPS = 10.0        # piso para a distância do stop loss em pips
MAX_LEVERAGE = 10.0       # teto de alavancagem (notional / equity)


def mt5_symbol(symbol: str) -> str:
    """Converte símbolo do app (EUR-USD) para o formato do MT5 (EURUSD)."""
    return symbol.replace("-", "")


def stop_distances(atr: float, sl_atr: float, tp_atr: float,
                   spread: float = 0.0, point: float = 0.0, tp_pips: float = 0.0, pip_size: float = 0.0001) -> tuple:
    """
    Distâncias efetivas de SL/TP em preço. Se o spread for maior que a
    distância por ATR, os stops caindo dentro do spread são rejeitados pela
    corretora ("Invalid stops"). Garante um piso = spread + 5 pontos.
    """
    atr_sl = max(atr, 0.0) * sl_atr
    if tp_pips > 0:
        atr_tp = tp_pips * pip_size
    else:
        atr_tp = max(atr, 0.0) * tp_atr
    floor = max(spread + 5.0 * max(point, 1e-6), 0.0)
    return max(atr_sl, floor), max(atr_tp, floor)


def clamp_size(size: float, price: float, equity: float,
               contract: float = 1.0) -> float:
    """
    Limita o tamanho de uma posição para evitar a explosão de alavancagem
    que ocorria quando o ATR (e portanto o SL) ficava minúsculo.

    `size` é em UNIDADES (paper) ou LOTES (MT5). `price` é o preço de entrada,
    `equity` o capital, `contract` o fator de conversão (1 p/ paper, CONTRACT_SIZE
    p/ MT5). Devolve o tamanho jamais maior que MAX_LEVERAGE × equity.
    """
    if price <= 0 or equity <= 0 or size <= 0:
        return size
    max_units = equity * MAX_LEVERAGE / price
    return min(size, max_units / max(contract, 1e-12))


class Broker(ABC):
    """Interface de execução — mesma para corretora simulada ou real."""

    @abstractmethod
    def place_order(self, symbol: str, side: int, price: float, sl: float, tp: float, size: float) -> bool:
        ...

    @abstractmethod
    def close_position(self, symbol: str, price: float) -> Optional[float]:
        ...

    @abstractmethod
    def get_positions(self) -> dict:
        ...


class PaperBroker(Broker):
    """Execução simulada com slippage e persistência em JSON."""

    def __init__(self, slippage_pips: float = 1.0, initial_capital: float = 10000.0,
                 state_file: str = STATE_FILE):
        self.slippage = slippage_pips * PIP_SIZE
        self.state_file = state_file
        self.state = self._load()
        if "capital" not in self.state:
            self.state["capital"] = initial_capital
            self.state["initial_capital"] = initial_capital
            self.state["positions"] = {}
            self.state["history"] = []
            self.state["equity_curve"] = []
            self._save()

    # ------------------------------------------------------------- helpers
    def _load(self) -> dict:
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception:
            pass

    def _mark(self, symbol: str, price: Optional[float] = None) -> None:
        """Registra ponto da curva de equity marcado a mercado (preço atual)."""
        equity = self.state["capital"]
        pos = self.state["positions"].get(symbol)
        if pos and price:
            equity += (price - pos["entry"]) * pos["side"] * pos["size"]
        self.state["equity_curve"].append({"ts": datetime.now().isoformat(), "equity": round(equity, 2)})
        if len(self.state["equity_curve"]) > 5000:
            self.state["equity_curve"] = self.state["equity_curve"][-2000:]

    # ------------------------------------------------------------ Broker API
    def place_order(self, symbol, side, price, sl, tp, size) -> bool:
        # slippage: piora o preço de entrada
        fill = price + side * self.slippage
        pos = {
            "side": side, "entry": round(fill, 5), "sl": round(sl, 5), "tp": round(tp, 5),
            "size": round(size, 4), "ts": datetime.now().isoformat(),
        }
        self.state["positions"][symbol] = pos
        self._save()
        return True

    def close_position(self, symbol, price) -> Optional[float]:
        pos = self.state["positions"].pop(symbol, None)
        if pos is None:
            return None
        fill = price - pos["side"] * self.slippage  # slippage piora a saída
        pnl = (fill - pos["entry"]) * pos["side"] * pos["size"]
        self.state["capital"] += pnl
        self.state["history"].append({
            "symbol": symbol, "side": "LONG" if pos["side"] == 1 else "SHORT",
            "entry": pos["entry"], "exit": round(fill, 5), "pnl": round(pnl, 2),
            "entry_ts": pos["ts"], "exit_ts": datetime.now().isoformat(),
        })
        self._save()
        return pnl

    def get_positions(self) -> dict:
        return self.state["positions"]

    # ------------------------------------------------------- lógica do app
    def on_signal(self, symbol: str, signal: int, price: float, atr: float,
                  sl_atr: float = 2.0, tp_atr: float = 2.0, risk_pct: float = 0.25, tp_pips: float = 0.0,
                  trailing_stop_pips: float = 0.0, dynamic_risk_margin: float = 0.0) -> str:
        """
        Recebe o sinal do dashboard + preço/ATR atuais e executa no modo demo.
        Retorna uma mensagem descrevendo o que foi feito.
        """
        pos = self.state["positions"].get(symbol)
        self._mark(symbol, price)

        # sem posição: abre se houver sinal
        if pos is None and signal != 0:
            if atr and atr > 0:
                sl_dist, tp_dist = stop_distances(atr, sl_atr, tp_atr, tp_pips=tp_pips, pip_size=PIP_SIZE)
                sl_dist = max(sl_dist, MIN_SL_PIPS * PIP_SIZE)
                tp_dist = max(tp_dist, MIN_SL_PIPS * PIP_SIZE)
                # Risco dinâmico
                applied_risk_pct = risk_pct
                if dynamic_risk_margin > 0:
                    risk_multiplier = 0.5 + (dynamic_risk_margin / 0.5) * 1.5
                    applied_risk_pct = risk_pct * min(max(risk_multiplier, 0.5), 2.0)

                size = (self.state["capital"] * applied_risk_pct / 100.0) / sl_dist
                size = clamp_size(size, price, self.state["capital"])
                entry = price
                sl = entry - signal * sl_dist
                tp = entry + signal * tp_dist
                self.place_order(symbol, signal, entry, sl, tp, size)
                return (f"🟢 ABRIU LONG em {symbol} @ {entry:.5f} (SL {sl:.5f}, TP {tp:.5f})"
                        if signal == 1 else
                        f"🔴 ABRIU SHORT em {symbol} @ {entry:.5f} (SL {sl:.5f}, TP {tp:.5f})")
            return "Sinal sem ATR válido — posição não aberta."

        # com posição: verifica SL/TP ou sinal contrário
        if pos is not None:
            side = pos["side"]
            
            # Trailing stop manual na execução paper
            if trailing_stop_pips > 0:
                if side == 1:
                    pos["sl"] = max(pos["sl"], price - trailing_stop_pips * PIP_SIZE)
                else:
                    pos["sl"] = min(pos["sl"], price + trailing_stop_pips * PIP_SIZE)
            
            if side == 1:
                hit_sl = price <= pos["sl"]
                hit_tp = price >= pos["tp"]
            else:
                hit_sl = price >= pos["sl"]
                hit_tp = price <= pos["tp"]

            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                pnl = self.close_position(symbol, price)
                return (f"✅ FECHOU posição {symbol} por {reason} @ {price:.5f} — "
                        f"PnL {pnl:+.2f} USD")
            if signal != 0 and signal != side:
                pnl = self.close_position(symbol, price)
                return (f"🔄 FECHOU {symbol} por sinal contrário @ {price:.5f} — "
                        f"PnL {pnl:+.2f} USD")

        return ""

    def summary(self) -> dict:
        """Resumo para exibir no app."""
        hist = self.state["history"]
        pnls = [t["pnl"] for t in hist]
        wins = [p for p in pnls if p > 0]
        capital = self.state["capital"]
        return {
            "capital": round(capital, 2),
            "initial_capital": round(self.state["initial_capital"], 2),
            "trades": len(hist),
            "wins": len(wins),
            "pnl_total": round(sum(pnls), 2),
            "return_pct": round((capital - self.state["initial_capital"]) / self.state["initial_capital"] * 100, 2),
        }

    def reset(self, initial_capital: float = 10000.0) -> None:
        self.state = {
            "capital": initial_capital,
            "initial_capital": initial_capital,
            "positions": {},
            "history": [],
            "equity_curve": [],
        }
        self._save()


class Mt5Broker(Broker):
    """
    Execução REAL no MetaTrader 5 (conta demo ou real) via bridge RPyC.

    Requer: terminal MT5 aberto/logado sob Wine + mt5server.exe rodando
    (ver README). Mesma interface do PaperBroker — o app só troca a instância.
    """

    def __init__(self, risk_pct: float = 0.25, sl_atr: float = 2.0, tp_atr: float = 2.0,
                 bridge=None):
        self._bridge = bridge
        self.risk_pct = risk_pct
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self._last_msg = ""

    # ------------------------------------------------------------ helpers
    def _mt5(self):
        if self._bridge is None:
            from mt5_bridge import MT5Bridge
            self._bridge = MT5Bridge()
        return self._bridge

    def available(self) -> bool:
        try:
            m = self._mt5()
            return m.available() and bool(m.initialize())
        except Exception:
            return False

    def _size_lots(self, equity: float, risk_pct: float, sl_distance: float,
                   price: float = 0.0) -> float:
        """Dimensiona volume em lotes para o risco pedido, com cap de alavancagem."""
        if sl_distance <= 0:
            return 0.01
        risk_amount = equity * risk_pct / 100.0
        lots = risk_amount / (sl_distance * CONTRACT_SIZE)
        lots = clamp_size(lots, price, equity, contract=CONTRACT_SIZE) if price > 0 else lots
        return max(0.01, round(lots, 2))

    # ------------------------------------------------------------ Broker API
    def place_order(self, symbol, side, price, sl, tp, size) -> bool:
        m = self._mt5()
        sym = mt5_symbol(symbol)
        q = m.quote(sym)
        if not q:
            return False
        m.initialize()
        res = m.place_market(sym, side, size, sl=sl, tp=tp)
        code = getattr(res, "retcode", None)
        self._last_msg = str(getattr(res, "comment", "")) or str(code)
        return code == 10009  # TRADE_RETCODE_DONE

    def close_position(self, symbol, price) -> Optional[float]:
        m = self._mt5()
        sym = mt5_symbol(symbol)
        m.initialize()
        before = [p for p in m.positions() if p["symbol"] == sym]
        if not before:
            return None
        pnl = sum(p["profit"] for p in before)
        res = m.close_symbol(sym)
        for r in res:
            code = getattr(r, "retcode", None)
            self._last_msg = str(getattr(r, "comment", "")) or str(code)
        return pnl

    def get_positions(self) -> dict:
        m = self._mt5()
        out = {}
        try:
            m.initialize()
            for p in m.positions():
                out[p["symbol"]] = {
                    "side": p["side"],
                    "entry": p["entry"],
                    "sl": p["sl"],
                    "tp": p["tp"],
                    "size": p["volume"],
                    "profit": p["profit"],
                    "ticket": p["ticket"],
                    "ts": "",
                }
        except Exception:
            pass
        return out

    # ------------------------------------------------------- lógica do app
    def on_signal(self, symbol: str, signal: int, price: float, atr: float,
                  sl_atr: float = 2.0, tp_atr: float = 2.0, risk_pct: float = 0.25, tp_pips: float = 0.0,
                  trailing_stop_pips: float = 0.0, dynamic_risk_margin: float = 0.0) -> str:
        m = self._mt5()
        sym = mt5_symbol(symbol)
        self.risk_pct = risk_pct
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        try:
            m.initialize()
        except Exception as e:
            return f"❌ MT5 indisponível: {e}"

        q = m.quote(sym)
        if not q:
            return f"❌ Símbolo {sym} não encontrado no MT5."
        live = q["ask"] if signal == 1 else q["bid"]
        spread = max(q["ask"] - q["bid"], 0.0)

        pos = [p for p in m.positions() if p["symbol"] == sym]
        ai = m.account_info()
        equity = ai.get("equity") or ai.get("balance") or 0.0

        # sem posição: abre se houver sinal
        if not pos and signal != 0:
            if atr and atr > 0:
                _ps = max(q.get("point", 0.0), 1e-6)
                _pip_size = 0.01 if "JPY" in sym or "XAU" in sym else 0.0001
                sl_dist, tp_dist = stop_distances(atr, sl_atr, tp_atr, spread, _ps, tp_pips=tp_pips, pip_size=_pip_size)
                sl_dist = max(sl_dist, MIN_SL_PIPS * max(q.get("point", 0.0), 1e-6))
                tp_dist = max(tp_dist, MIN_SL_PIPS * max(q.get("point", 0.0), 1e-6))
                sl = live - signal * sl_dist
                tp = live + signal * tp_dist
                # Risco dinâmico
                applied_risk_pct = risk_pct
                if dynamic_risk_margin > 0:
                    risk_multiplier = 0.5 + (dynamic_risk_margin / 0.5) * 1.5
                    applied_risk_pct = risk_pct * min(max(risk_multiplier, 0.5), 2.0)

                size = self._size_lots(equity, applied_risk_pct, sl_dist, price=live)
                ok = self.place_order(symbol, signal, live, sl, tp, size)
                if ok:
                    return (f"🟢 ABRIU LONG no MT5 {sym} @ {live:.5f} "
                            f"(SL {sl:.5f}, TP {tp:.5f}, {size} lotes)"
                            if signal == 1 else
                            f"🔴 ABRIU SHORT no MT5 {sym} @ {live:.5f} "
                            f"(SL {sl:.5f}, TP {tp:.5f}, {size} lotes)")
                return f"❌ MT5 recusou a ordem: {self._last_msg}"
            return "Sinal sem ATR válido — posição não aberta."

        # com posição: verifica SL/TP ou sinal contrário
        if pos:
            p = pos[0]
            side = p["side"]
            if side == 1:
                hit_sl = live <= p["sl"] and p["sl"] > 0
                hit_tp = live >= p["tp"] and p["tp"] > 0
            else:
                hit_sl = live >= p["sl"] and p["sl"] > 0
                hit_tp = live <= p["tp"] and p["tp"] > 0

            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                pnl = self.close_position(symbol, live)
                return (f"✅ FECHOU {sym} por {reason} no MT5 @ {live:.5f} — "
                        f"PnL {pnl:+.2f}")
            if signal != 0 and signal != side:
                pnl = self.close_position(symbol, live)
                return (f"🔄 FECHOU {sym} por sinal contrário no MT5 @ {live:.5f} — "
                        f"PnL {pnl:+.2f}")

        return ""

    def summary(self) -> dict:
        m = self._mt5()
        try:
            m.initialize()
        except Exception:
            return {"error": "MT5 indisponível"}
        ai = m.account_info()
        pos = m.positions()
        return {
            "capital": ai.get("equity"),
            "initial_capital": ai.get("balance"),
            "trades": 0,
            "wins": 0,
            "pnl_total": ai.get("profit"),
            "return_pct": None,
            "login": ai.get("login"),
            "server": ai.get("server"),
            "company": ai.get("company"),
            "open_positions": len(pos),
        }

    def reset(self, initial_capital: float = 10000.0) -> None:
        m = self._mt5()
        m.initialize()
        for sym in {p["symbol"] for p in m.positions()}:
            m.close_symbol(sym)
