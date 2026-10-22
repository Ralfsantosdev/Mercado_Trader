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

from safety import (
    KILL_SWITCH_FILE,
    atomic_write_json,
    disable_trading,
    load_json,
    trading_allowed,
    warn,
)

# Dimensionamento pelo risco real do símbolo (tick_size/tick_value do MT5)
try:
    from pullback_liquidity_strategy import position_size_by_risk as _pls_size, risk_guard as _pls_risk_guard
    _PLS_SIZE_AVAILABLE = True
except ImportError:
    _PLS_SIZE_AVAILABLE = False
    _pls_risk_guard = None

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execution_state.json")

PIP_SIZE = 0.0001  # maioria dos pares; JPY usa 0.01 (ajustar se operar JPY)

CONTRACT_SIZE = 100_000  # 1 lote padrão = 100k unidades da base (forex)

# Limites de segurança anti-implosão de posição (foram a causa do trade de -225)
MIN_SL_PIPS = 10.0        # piso para a distância do stop loss em pips
MAX_LEVERAGE = 10.0       # teto de alavancagem (notional / equity)

# Magic do robô — mesma constante do mt5_bridge. Toda leitura/escrita de posição
# usa ela para NÃO tocar em trades manuais nem de outros EAs da conta.
ROBOT_MAGIC = 20260805

# ── Guarda de conta REAL ──────────────────────────────────────────────────────
# `trade_mode` do MT5: 0=real, 1=demo, 2=contest. O sistema opera em real SÓ
# se ALLOW_LIVE_TRADING=1 (ou "true"/"yes") estiver no ambiente. Sem isso,
# qualquer teste em conta real é bloqueado antes de qualquer order_send.
LIVE_TRADING_ENV = "ALLOW_LIVE_TRADING"
_TRUTHY = {"1", "true", "yes", "on", "sim"}


def live_trading_enabled() -> bool:
    return os.environ.get(LIVE_TRADING_ENV, "").strip().lower() in _TRUTHY


def account_gate(account_mode: str) -> tuple:
    """
    (permitido, motivo) — decide se PODE operar na conta conectada.

    ALLOWLIST: só `demo` e `contest` passam sem mais nada. Qualquer outra
    coisa — `real`, `unknown`, string inesperada, `None` — fica BLOQUEADA.
    `real` exige ALLOW_LIVE_TRADING=1 explícito.

    O mapeamento vem de `MT5Bridge.account_mode()`, que lê
    ACCOUNT_TRADE_MODE (0=demo, 1=contest, 2=real) e ainda cruza com o nome
    do servidor: qualquer indício de conta live/real vira `real` aqui.
    """
    mode = (account_mode or "unknown").lower().strip()
    if mode in ("demo", "contest"):
        return True, ""
    if mode == "real":
        if live_trading_enabled():
            return True, ""
        return False, (f"conta REAL detectada e {LIVE_TRADING_ENV} não definido. "
                       f"Para operar dinheiro de verdade exporte "
                       f"{LIVE_TRADING_ENV}=1 (README → Conta real).")
    return False, f"modo da conta desconectado ({account_mode}) — operação bloqueada por segurança."


def mt5_symbol(symbol: str) -> str:
    """Converte símbolo do app (EUR-USD, EURUSD=X) para o formato do MT5 (EURUSD)."""
    s = symbol.replace("=X", "").upper()
    return s.replace("-", "")


def stop_distances(atr: float, sl_atr: float, tp_atr: float,
                   spread: float = 0.0, point: float = 0.0, tp_pips: float = 0.0,
                   pip_size: float = 0.0001, vol_mult: float = 1.0) -> tuple:
    """
    Distâncias efetivas de SL/TP em preço. Se o spread for maior que a
    distância por ATR, os stops caindo dentro do spread são rejeitados pela
    corretora ("Invalid stops"). Garante um piso = spread + 5 pontos.

    `vol_mult` = multiplicador do regime de volatilidade (Vol_Stop_Mult):
    alarga SL/TP em ATR quando a vol está acima da própria mediana histórica
    (rajada que o ATR(14) ainda não refletiu). Nunca aperta — mínimo 1.0.
    """
    mult = float(vol_mult) if vol_mult and vol_mult > 0 else 1.0
    atr_sl = max(atr, 0.0) * sl_atr * mult
    if tp_pips > 0:
        atr_tp = tp_pips * pip_size
    else:
        atr_tp = max(atr, 0.0) * tp_atr * mult
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
        # load_json nunca devolve {} para um arquivo VÁLIDO — o `except: pass`
        # antigo reinicializava capital e apagava o histórico sem avisar.
        data = load_json(self.state_file, {})
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            atomic_write_json(self.state_file, self.state)
        except Exception as e:
            warn(f"execution_state: falha ao gravar {self.state_file}: {e}")

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
                  trailing_stop_pips: float = 0.0, dynamic_risk_margin: float = 0.0,
                  max_open_positions: int = 1,
                  suggested_sl: float = 0.0, suggested_tp: float = 0.0,
                  pip_size: float = PIP_SIZE, vol_mult: float = 1.0) -> str:
        """
        Recebe o sinal do dashboard + preço/ATR atuais e executa no modo demo.
        Retorna uma mensagem descrevendo o que foi feito.

        `suggested_sl`/`suggested_tp`: stops explícitos vindos da estratégia
        (ex.: Fimathe — SL/TP por tamanho de canal). Quando > 0, são usados
        exatamente como o backtest faz; senão cai no SL/TP por ATR.
        `vol_mult`: multiplicador do regime de volatilidade (1.0 = normal).
        """
        pos = self.state["positions"].get(symbol)
        self._mark(symbol, price)

        # sem posição: abre se houver sinal
        if pos is None and signal != 0:
            current_open = len(self.state.get("positions", {}))
            if current_open >= max_open_positions:
                return f"⚠️ Limite de portfólio atingido ({current_open}/{max_open_positions} posições). Sinal ignorado."
                
            if _pls_risk_guard:
                today_str = datetime.now().isoformat()[:10]
                today_hist = [t for t in self.state.get("history", []) if t.get("exit_ts", "").startswith(today_str)]
                profits = [t["pnl"] for t in today_hist]
                daily_pnl = sum(profits)
                daily_pnl_pct = (daily_pnl / self.state["initial_capital"] * 100) if self.state["initial_capital"] > 0 else 0
                
                consecutive_losses = 0
                for p in reversed(profits):
                    if p < 0:
                        consecutive_losses += 1
                    elif p > 0:
                        break
                
                ok, reason = _pls_risk_guard(
                    current_daily_pnl_pct=daily_pnl_pct,
                    consecutive_losses=consecutive_losses,
                    has_open_position=current_open > 0,
                )
                if not ok:
                    return f"🛡️ Entrada bloqueada: {reason}"

            if atr and atr > 0:
                # Risco dinâmico (comum aos dois caminhos de stop)
                applied_risk_pct = risk_pct
                if dynamic_risk_margin > 0:
                    risk_multiplier = 0.5 + (dynamic_risk_margin / 0.5) * 1.5
                    applied_risk_pct = risk_pct * min(max(risk_multiplier, 0.5), 2.0)

                if suggested_sl > 0 and suggested_tp > 0:
                    # stops explícitos da estratégia (Fimathe) — iguais ao backtest
                    entry = price
                    sl = float(suggested_sl)
                    tp = float(suggested_tp)
                    sl_dist = max(abs(entry - sl), MIN_SL_PIPS * max(pip_size, 1e-6))
                else:
                    sl_dist, tp_dist = stop_distances(atr, sl_atr, tp_atr, tp_pips=tp_pips,
                                                      pip_size=pip_size, vol_mult=vol_mult)
                    sl_dist = max(sl_dist, MIN_SL_PIPS * max(pip_size, 1e-6))
                    tp_dist = max(tp_dist, MIN_SL_PIPS * max(pip_size, 1e-6))

                size = (self.state["capital"] * applied_risk_pct / 100.0) / sl_dist
                size = clamp_size(size, price, self.state["capital"])
                entry = price
                if suggested_sl <= 0 or suggested_tp <= 0:
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
    Execução REAL (ou demo) no MetaTrader 5 via bridge RPyC.

    Requer: terminal MT5 aberto/logado sob Wine + mt5server.exe rodando (ver README).

    Barreiras de segurança implementadas nesta classe:
      * kill-switch (arquivo TRADING_DISABLED) — bloqueia ENTRADA, nunca proteção;
      * gate demo/real (ALLOW_LIVE_TRADING) antes de qualquer order_send;
      * `ensure_ready()` — initialize() True + conta logada + equity > 0;
      * filtragem por ROBOT_MAGIC — não toca em trades manuais/outras EAs;
      * order_check + margem livre antes de enviar;
      * risk_guard fail-safe quando o histórico do dia não puder ser lido;
      * limite diário de perda que DESLIGA o robô sozinho (persistente);
      * trailing stop real via TRADE_ACTION_SLTP.
    """

    def __init__(self, risk_pct: float = 0.25, sl_atr: float = 2.0, tp_atr: float = 2.0,
                 bridge=None):
        self._bridge = bridge
        self.risk_pct = risk_pct
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self._last_msg = ""
        # Resultado da última ordem, para o chamador ligar o journal/Telegram:
        # {"action": "open"|"close", symbol, side, price, sl, tp, size, ticket, pnl, reason}
        self.last_order: Optional[dict] = None

    # ------------------------------------------------------------ helpers
    def _mt5(self):
        if self._bridge is None:
            from mt5_bridge import get_bridge
            self._bridge = get_bridge()
        return self._bridge

    def available(self) -> bool:
        """Só True se a ponte viver E a conta estiver logada com equity > 0."""
        try:
            ok, _ = self._mt5().ensure_ready()
            return ok
        except Exception:
            return False

    def unavailable_reason(self) -> str:
        try:
            return self._mt5().last_error or "MT5 indisponível"
        except Exception as e:
            return str(e)

    def account_mode(self) -> str:
        try:
            return self._mt5().account_mode()
        except Exception:
            return "unknown"

    def _size_lots(self, equity: float, risk_pct: float, sl_distance: float,
                   price: float = 0.0, symbol: str = "") -> float:
        """
        Dimensiona volume em lotes para o risco pedido.

        Devolve 0.0 quando NÃO SE DEVE OPERAR:
          - equity <= 0 (conta desconectada/sessão morta);
          - sl_distance <= 0;
          - a política de risco recusou (lote mínimo já estouraria o risco).

        O comportamento antigo era fazer `max(0.01, ...)` nesses casos — ou seja,
        mandava ordem de 0.01 mesmo quando o sistema disse "não opere".
        """
        if equity <= 0:
            warn(f"sizing recusado: equity={equity}")
            return 0.0
        if sl_distance <= 0:
            return 0.0

        # Tenta obter info do contrato via bridge
        if symbol and _PLS_SIZE_AVAILABLE:
            try:
                m = self._mt5()
                info = m.symbol_info(mt5_symbol(symbol)) if hasattr(m, "symbol_info") else {}
                if info and info.get("trade_tick_size", 0) > 0 and info.get("trade_tick_value", 0) > 0:
                    lots = _pls_size(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry=price,
                        stop=price - sl_distance,  # direção não importa, só a distância
                        tick_size=info["trade_tick_size"],
                        tick_value=info["trade_tick_value"],
                        volume_min=info.get("volume_min", 0.01),
                        volume_max=info.get("volume_max", 100.0),
                        volume_step=info.get("volume_step", 0.01),
                    )
                    if lots is not None and float(lots) <= 0:
                        # 0.0 = "o lote mínimo já arriscaria mais do que o
                        # permitido" → respeita a política, não força entrada.
                        return 0.0
                    if lots and float(lots) > 0:
                        return float(lots)
            except Exception as e:
                warn(f"sizing exato indisponível ({e}) — usando fórmula simplificada")

        # Fórmula simplificada (CONTRACT_SIZE fixo) — fallback
        risk_amount = equity * risk_pct / 100.0
        lots = risk_amount / (sl_distance * CONTRACT_SIZE)
        lots = clamp_size(lots, price, equity, contract=CONTRACT_SIZE) if price > 0 else lots
        lots = round(lots, 2)
        if lots < 0.01:
            return 0.0  # não há como respeitar o risco com o lote mínimo
        return lots

    # ------------------------------------------------------------ Broker API
    def place_order(self, symbol, side, price, sl, tp, size) -> bool:
        m = self._mt5()
        sym = mt5_symbol(symbol)
        if not size or float(size) <= 0:
            self._last_msg = "volume calculado = 0 (risco acima do permitido)"
            return False
        q = m.quote(sym)
        if not q:
            self._last_msg = f"sem cotação de {sym}"
            return False
        ok, why = m.ensure_ready()
        if not ok:
            self._last_msg = why
            return False
        res = m.place_market(sym, side, float(size), sl=sl, tp=tp)
        code = res.get("retcode")
        text = res.get("code_text", "")
        self._last_msg = (res.get("comment") or text or str(code))
        done = code == 10009
        if done:
            self.last_order = {
                "action": "open", "symbol": symbol, "mt5_symbol": sym,
                "side": int(side), "price": float(res.get("price") or price),
                "sl": float(sl), "tp": float(tp), "size": float(size),
                "ticket": int(res.get("order") or 0),
            }
        else:
            self._last_msg = f"{self._last_msg} [{text}]"
        return done

    def close_position(self, symbol, price, reason: str = "") -> Optional[float]:
        """
        Fecha as posições do robô no símbolo. Devolve o PnL REALIZADO (lido
        DEPOIS do fill) ou None se nada foi fechado.
        """
        m = self._mt5()
        sym = mt5_symbol(symbol)
        ok, why = m.ensure_ready()
        if not ok:
            self._last_msg = why
            return None
        before = [p for p in m.positions() if p["symbol"] == sym]
        if not before:
            return None
        pnl = 0.0
        closed_any = False
        for p in before:
            r = m.close_ticket(p["ticket"])
            if r.get("retcode") == 10009:
                closed_any = True
                pnl += float(p.get("profit") or 0.0)
            else:
                self._last_msg = f"fechamento recusado: {r.get('comment')} [{r.get('code_text')}]"
        if not closed_any:
            return None
        self.last_order = {
            "action": "close", "symbol": symbol, "mt5_symbol": sym,
            "price": float(price), "pnl": float(pnl), "reason": reason,
        }
        return pnl

    def get_positions(self) -> dict:
        """Só posições do ROBOT_MAGIC — não mostra nem conta as manuais."""
        m = self._mt5()
        out = {}
        try:
            if not m.initialize():
                return out
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
        except Exception as e:
            warn(f"get_positions: {e}")
        return out

    def positions_for(self, symbol: str) -> list:
        """Posições do robô num símbolo específico (formato MT5)."""
        try:
            m = self._mt5()
            return [p for p in m.positions() if p["symbol"] == mt5_symbol(symbol)]
        except Exception as e:
            warn(f"positions_for: {e}")
            return []

    # ------------------------------------------------- risco / proteções
    def _daily_risk(self, m, equity: float) -> tuple:
        """
        (pnl_pct, consecutive_losses, blocked_reason).

        fail-safe: se o histórico de hoje não puder ser lido (`None`), considera
        BLOQUEADO — antes o erro virava [] = "0 perdas" = liberava tudo.
        """
        deals = m.history_deals_today() if hasattr(m, "history_deals_today") else None
        if deals is None:
            return 0.0, 0, ("histórico do dia indisponível "
                            f"({getattr(m, 'last_error', '')or 'erro'}) — entradas bloqueadas")
        profits = [d.get("profit", 0.0) for d in deals]
        daily_pnl = sum(profits)
        daily_pnl_pct = (daily_pnl / equity * 100) if equity > 0 else 0.0
        consecutive = 0
        for p in reversed(profits):
            if p < 0:
                consecutive += 1
            elif p > 0:
                break
        return daily_pnl_pct, consecutive, ""

    def _trailing(self, m, pos: dict, live: float, trailing_stop_pips: float,
                  pip_size: float, point: float) -> Optional[str]:
        """Move o SL para trás do preço. Retorna mensagem se moveu/falhou."""
        if trailing_stop_pips <= 0:
            return None
        dist = trailing_stop_pips * pip_size
        if dist <= 0:
            return None
        side = pos["side"]
        cur_sl = float(pos.get("sl") or 0.0)
        if side == 1:
            new_sl = live - dist
            # só anda pra cima (trailing), nunca solta o stop
            if cur_sl > 0 and new_sl <= cur_sl:
                return None
            if pos["entry"] + dist > new_sl and cur_sl <= 0:
                return None  # ainda não deu break-even: deixa o stop original
        else:
            new_sl = live + dist
            if cur_sl > 0 and new_sl >= cur_sl:
                return None
            if pos["entry"] - dist < new_sl and cur_sl <= 0:
                return None
        r = m.modify_position_stops(pos["ticket"], pos["symbol"], new_sl, float(pos.get("tp") or 0.0))
        if r.get("retcode") == 10009:
            return f"↕️ Trailing {pos['symbol']}: SL -> {new_sl:.5f}"
        return f"⚠️ Trailing falhou ({r.get('code_text')})"

    # ------------------------------------------------------- lógica do app
    def on_signal(self, symbol: str, signal: int, price: float, atr: float,
                  sl_atr: float = 2.0, tp_atr: float = 2.0, risk_pct: float = 0.25, tp_pips: float = 0.0,
                  trailing_stop_pips: float = 0.0, dynamic_risk_margin: float = 0.0,
                  max_open_positions: int = 1,
                  suggested_sl: float = 0.0, suggested_tp: float = 0.0,
                  pip_size: float = 0.0001, vol_mult: float = 1.0) -> str:
        m = self._mt5()
        sym = mt5_symbol(symbol)
        self.risk_pct = risk_pct
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self.last_order = None

        ok, why = m.ensure_ready()
        if not ok:
            return f"❌ MT5 indisponível: {why}"

        # gate demo/real — ANTES de qualquer decisão de compra/venda
        mode = m.account_mode()
        can_trade, gate_reason = account_gate(mode)
        # gate do kill-switch (impede ENTRADA; fechamento/proteção continua ok)
        kill_ok, kill_reason = trading_allowed()

        q = m.quote(sym)
        if not q or not q.get("ask"):
            return f"❌ Símbolo {sym} não encontrado no MT5."
        live = q["ask"] if signal == 1 else q["bid"]
        spread = max(q["ask"] - q["bid"], 0.0)

        pos = [p for p in m.positions() if p["symbol"] == sym]  # já filtrado por magic
        ai = m.account_info()
        equity = float(ai.get("equity") or 0.0)

        # ── gestão da posição aberta (sempre permitida, mesmo com kill-switch)
        if pos:
            # trailing stop real (TRADE_ACTION_SLTP)
            if trailing_stop_pips > 0:
                _ps_t = 0.01 if ("JPY" in sym or "XAU" in sym) else (
                    1.0 if any(k in sym for k in ("US500", "NAS100", "US30", "GER40", "IBOV")) else 0.0001)
                if pip_size > 0:
                    _ps_t = pip_size
                msg = self._trailing(m, pos[0], live, trailing_stop_pips, _ps_t,
                                     float(q.get("point") or 0.0))
                if msg:
                    return msg

            p = pos[0]
            side = p["side"]
            if side == 1:
                hit_sl = p["sl"] > 0 and live <= p["sl"]
                hit_tp = p["tp"] > 0 and live >= p["tp"]
            else:
                hit_sl = p["sl"] > 0 and live >= p["sl"]
                hit_tp = p["tp"] > 0 and live <= p["tp"]

            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                pnl = self.close_position(symbol, live, reason=reason)
                if pnl is None:
                    return f"⚠️ SL/TP atingido mas o fechamento foi recusado: {self._last_msg}"
                return (f"✅ FECHOU {sym} por {reason} no MT5 @ {live:.5f} — "
                        f"PnL {pnl:+.2f}")
            if signal != 0 and signal != side:
                pnl = self.close_position(symbol, live, reason="sinal_contrario")
                if pnl is None:
                    return f"⚠️ Sinal contrário, fechamento recusado: {self._last_msg}"
                return (f"🔄 FECHOU {sym} por sinal contrário no MT5 @ {live:.5f} — "
                        f"PnL {pnl:+.2f}")
            return ""  # posição gerida, sem ação

        # ── sem posição: só ABRE se todas as barreiras permitirem
        if signal == 0:
            return ""

        if not kill_ok:
            return f"🛑 Entrada bloqueada — trading desativado ({kill_reason})"
        if not can_trade:
            return f"🛑 Entrada bloqueada — {gate_reason}"

        current_open = len(m.positions())
        if current_open >= max_open_positions:
            return (f"⚠️ Limite de portfólio atingido ({current_open}/{max_open_positions}) "
                    f"posições do robô. Sinal ignorado.")
        if equity <= 0:
            return "❌ Equity inválido na conta — sessão de trading inativa."

        # limite diário de perda + sequência de losses (fail-safe)
        daily_pct, consec, blocked = self._daily_risk(m, equity)
        if blocked:
            return f"🛡️ Entrada bloqueada: {blocked}"
        if _pls_risk_guard:
            ok_g, reason_g = _pls_risk_guard(
                current_daily_pnl_pct=daily_pct,
                consecutive_losses=consec,
                has_open_position=current_open > 0,
            )
            if not ok_g:
                if reason_g == "daily_loss_limit":
                    # persiste o corte: o robô NÃO volta a operar no dia seguinte
                    # por conta de um cron esquecido. Alguém precisa ligar de novo.
                    disable_trading(f"limite diário de perda atingido ({daily_pct:.2f}%)")
                    return (f"🛑 LIMITE DIÁRIO DE PERDA ({daily_pct:.2f}%): trading "
                            f"DESATIVADO em {os.path.basename(KILL_SWITCH_FILE)}")
                return f"🛡️ Entrada bloqueada: {reason_g}"

        if not (atr and atr > 0):
            return "Sinal sem ATR válido — posição não aberta."

        # margem livre mínima: sem margem a ordem seria 10019 de qualquer forma
        margin_free = float(ai.get("margin_free") or 0.0)
        if margin_free <= 0:
            return "❌ Sem margem livre na conta — entrada bloqueada."

        # risco dinâmico (Kelly fracionado pela margem do ML)
        applied_risk_pct = risk_pct
        if dynamic_risk_margin > 0:
            risk_multiplier = 0.5 + (float(dynamic_risk_margin) / 0.5) * 1.5
            applied_risk_pct = risk_pct * min(max(risk_multiplier, 0.5), 2.0)

        _ps = max(float(q.get("point") or 0.0), 1e-6)
        _indices = ("US500", "NAS100", "US30", "GER40", "IBOV")
        if "JPY" in sym or "XAU" in sym:
            _pip_size = 0.01
        elif any(k in sym for k in _indices):
            _pip_size = 1.0
        else:
            _pip_size = 0.0001
        if pip_size > 0:
            _pip_size = pip_size

        if suggested_sl > 0 and suggested_tp > 0:
            # stops explícitos da estratégia (Fimathe / PLS)
            sl = float(suggested_sl)
            tp = float(suggested_tp)
            sl_dist = max(abs(live - sl), MIN_SL_PIPS * _ps)
        else:
            sl_dist, tp_dist = stop_distances(atr, sl_atr, tp_atr, spread, _ps, tp_pips=tp_pips,
                                              pip_size=_pip_size, vol_mult=vol_mult)
            sl_dist = max(sl_dist, MIN_SL_PIPS * _ps)
            tp_dist = max(tp_dist, MIN_SL_PIPS * _ps)
            sl = live - signal * sl_dist
            tp = live + signal * tp_dist

        size = self._size_lots(equity, applied_risk_pct, sl_dist, price=live, symbol=symbol)
        if not size or size <= 0:
            return (f"⛔ Tamanho de posição = 0 (risco de {applied_risk_pct:.2f}% não "
                    f"cabe com o lote mínimo) — nada foi enviado.")

        ok_place = self.place_order(symbol, signal, live, sl, tp, size)
        if ok_place:
            side_txt = "LONG" if signal == 1 else "SHORT"
            msg = (f"🟢 ABRIU {side_txt} no MT5 {sym} @ {live:.5f} "
                   f"(SL {sl:.5f}, TP {tp:.5f}, {size} lotes)")
            if mode == "real":
                msg += "  ⚠️ CONTA REAL"
            return msg
        return f"❌ MT5 recusou a ordem: {self._last_msg}"

    def summary(self) -> dict:
        m = self._mt5()
        ok, why = m.ensure_ready()
        if not ok:
            return {"error": why}
        ai = m.account_info()
        pos = m.positions()  # só do robô
        balance = float(ai.get("balance") or 0.0)
        equity = float(ai.get("equity") or 0.0)

        trades = wins = 0
        realized = 0.0
        deals = None
        try:
            if hasattr(m, "history_deals_get"):
                deals = m.history_deals_get(days=30)
        except Exception as e:
            warn(f"summary deals: {e}")
        if deals:
            # deals do robô já vêm filtrados por magic; conta só entradas de ida
            entries = [d for d in deals if int(d.get("entry") or 0) in (0, 1)
                       and float(d.get("volume") or 0) > 0]
            trades = len(entries)
            realized = sum(float(d.get("profit") or 0.0) for d in deals)
            wins = len([d for d in entries if float(d.get("profit") or 0.0) > 0])

        return {
            "capital": equity,
            "balance": balance,
            "initial_capital": balance,
            "trades": trades,
            "wins": wins,
            "pnl_total": realized if deals is not None else float(ai.get("profit") or 0.0),
            "return_pct": (realized / balance * 100.0) if (balance > 0 and deals is not None) else None,
            "login": ai.get("login"),
            "server": ai.get("server"),
            "company": ai.get("company"),
            "trade_mode": ai.get("trade_mode"),
            "account_mode": self.account_mode(),
            "margin_free": ai.get("margin_free"),
            "open_positions": len(pos),
        }

    def reset(self, initial_capital: float = 10000.0) -> None:
        """
        Fecha APENAS as posições do robô (magic), nunca as manuais.
        Exige kill-switch para conta real — apertar "Zerar conta" num botão de UI
        não deveria destruir posições de dinheiro real sem confirmação.
        """
        m = self._mt5()
        ok, why = m.ensure_ready()
        if not ok:
            warn(f"reset: {why}")
            return
        mode = m.account_mode()
        if mode == "real" and not live_trading_enabled():
            warn("reset ignorado: conta real exige ALLOW_LIVE_TRADING=1")
            return
        for p in m.positions():
            r = m.close_ticket(p["ticket"])
            if r.get("retcode") != 10009:
                warn(f"reset: não fechou ticket {p['ticket']} — {r.get('code_text')}")
