
"""
Diário de operações — auditoria quantitativa completa.

Cada trade registra não só entrada/saída, mas TODOS os metadados do sinal
que permitam responder perguntas matemáticas depois:

  - Qual MAE médio dos vencedores?
  - Quantos trades chegaram a +1R antes de tomar stop?
  - Quantos atingiram 2R?
  - Qual ADX produz maior expectativa?
  - Qual profundidade de pullback funciona melhor?
  - O sweep realmente melhora a estratégia?
  - Qual spread médio das entradas ruins?

Persistência em JSON local. Compatível retroativamente com entradas antigas
(campos ausentes recebem None/0 quando não disponíveis).

Schema de uma entrada:
    signal_time     ISO-8601 do candle que gerou o sinal
    symbol          par / ativo
    side            +1 (buy) ou -1 (sell)
    entry           preço de entrada real (com slippage)
    sl              stop loss
    tp              take profit
    volume          lotes / unidades
    risk_pct        % do capital arriscado
    atr             ATR no momento do sinal
    adx             ADX no momento do sinal
    liquidity_level nível de liquidez varrido (Prior_High ou Prior_Low)
    sweep_extreme   extremo do candle de sweep (Low para buy, High para sell)
    trigger_reason  texto livre (ex: "trend+pullback+sweep+reclaim")
    spread_entry    spread (em pontos/pips) no momento da entrada
    slippage_entry  slippage real aplicado (em pontos/pips)

    status          "open" | "closed" | "cancelled"
    exit_time       ISO-8601 do fechamento
    exit_price      preço de saída real
    exit_reason     "SL" | "TP" | "reversal" | "manual" | "timeout"

    pnl_cash        PnL em dinheiro (positivo = lucro)
    result_r        resultado em múltiplos de R (ex: +2.0, -1.0)

    mae_price       Máxima Excursão Adversa em preço (pior ponto contra)
    mfe_price       Máxima Excursão Favorável em preço (melhor ponto a favor)

    # Campos legados (retrocompatibilidade)
    entry_price     alias de entry
    exit_price_old  alias de exit_price
    followed_plan   bool (campo do schema antigo)
    zone            zona SMC (campo do schema antigo)
    confirmation    confirmação textual (campo antigo)
    comment         comentário livre
"""

import json
import os
from datetime import datetime
from typing import Optional

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_state.json")

# ── Zonas SMC (retrocompatibilidade com schema antigo) ────────────────────────
ZONE_LIQUIDEZ = "liquidez"
ZONE_NEUTRA = "neutra"
ZONE_ARMADILHA = "armadilha"
ZONES = [ZONE_LIQUIDEZ, ZONE_NEUTRA, ZONE_ARMADILHA]
ZONE_LABEL = {
    ZONE_LIQUIDEZ: "🕳️ Zona de liquidez",
    ZONE_NEUTRA: "⚪ Zona neutra",
    ZONE_ARMADILHA: "🐍 Armadilha",
}


# ─────────────────────────────────────────────────────────────────────────────
# Persistência
# ─────────────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    from safety import load_json
    data = load_json(STATE_FILE, {"entries": []})
    if not isinstance(data, dict) or "entries" not in data:
        return {"entries": []}
    return data


def _save_state(state: dict) -> None:
    # Gravação atômica: antes, um 'w' truncado + except:pass podia apagar o
    # diário inteiro no meio de uma escrita.
    from safety import atomic_write_json
    try:
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        from safety import warn
        warn(f"journal: falha ao gravar {STATE_FILE}: {e}")


def _migrate_entry(e: dict) -> dict:
    """
    Garante que entradas do schema antigo tenham todos os novos campos.
    Nunca modifica campos que já existem.
    """
    defaults = {
        "signal_time": e.get("ts", None),
        "symbol": e.get("symbol", ""),
        "side": e.get("side", 0),
        "entry": e.get("entry_price", e.get("entry", None)),
        "sl": e.get("sl", None),
        "tp": e.get("tp", None),
        "volume": e.get("size", e.get("volume", 0.0)),
        "risk_pct": e.get("risk_pct", None),
        "atr": e.get("atr", None),
        "adx": e.get("adx", None),
        "liquidity_level": e.get("liquidity_level", None),
        "sweep_extreme": e.get("sweep_extreme", None),
        "trigger_reason": e.get("confirmation", e.get("trigger_reason", "")),
        "spread_entry": e.get("spread_entry", None),
        "slippage_entry": e.get("slippage_entry", None),
        "status": e.get("status", "closed"),
        "exit_time": e.get("exit_time", e.get("ts", None)),
        "exit_price": e.get("exit_price", e.get("entry_price", None)),
        "exit_reason": e.get("exit_reason", "manual"),
        "pnl_cash": e.get("pnl_cash", None),
        "result_r": e.get("result_r", None),
        "mae_price": e.get("mae_price", None),
        "mfe_price": e.get("mfe_price", None),
        # campos legados preservados
        "followed_plan": e.get("followed_plan", False),
        "zone": e.get("zone", ZONE_LIQUIDEZ),
        "comment": e.get("comment", ""),
    }
    for k, v in defaults.items():
        if k not in e:
            e[k] = v
    return e


# ─────────────────────────────────────────────────────────────────────────────
# API principal
# ─────────────────────────────────────────────────────────────────────────────

def open_trade(
    symbol: str,
    side: int,
    entry: float,
    sl: float,
    tp: float,
    volume: float = 0.0,
    risk_pct: float = 0.0,
    atr: float = 0.0,
    adx: float = 0.0,
    liquidity_level: float = 0.0,
    sweep_extreme: float = 0.0,
    trigger_reason: str = "",
    spread_entry: float = 0.0,
    slippage_entry: float = 0.0,
    signal_time: Optional[str] = None,
    zone: str = ZONE_LIQUIDEZ,
    comment: str = "",
) -> dict:
    """
    Registra a abertura de uma nova operação (status='open').

    Deve ser chamado logo após a execução da ordem. Campos de saída
    (exit_time, exit_price, pnl_cash, result_r, mae_price, mfe_price)
    são preenchidos ao fechar com close_trade().
    """
    state = _load_state()
    now = datetime.now().isoformat()
    record = {
        "signal_time": signal_time or now,
        "symbol": symbol,
        "side": int(side),
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "volume": float(volume),
        "risk_pct": float(risk_pct),
        "atr": float(atr) if atr else None,
        "adx": float(adx) if adx else None,
        "liquidity_level": float(liquidity_level) if liquidity_level else None,
        "sweep_extreme": float(sweep_extreme) if sweep_extreme else None,
        "trigger_reason": trigger_reason,
        "spread_entry": float(spread_entry) if spread_entry else None,
        "slippage_entry": float(slippage_entry) if slippage_entry else None,
        "status": "open",
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_cash": None,
        "result_r": None,
        "mae_price": None,
        "mfe_price": None,
        # legado
        "followed_plan": True,
        "zone": zone if zone in ZONES else ZONE_LIQUIDEZ,
        "comment": comment,
        "ts": now,
    }
    state.setdefault("entries", []).append(record)
    _save_state(state)
    return record


def close_trade(
    idx: int,
    exit_price: float,
    exit_reason: str = "manual",
    pnl_cash: float = 0.0,
    mae_price: Optional[float] = None,
    mfe_price: Optional[float] = None,
    comment: str = "",
) -> Optional[dict]:
    """
    Fecha uma entrada aberta (status='open') pelo índice na lista.

    Calcula result_r automaticamente a partir de entry/sl do sinal original.
    mae_price / mfe_price são opcionais — preencha se o MT5 ou o paper broker
    rastreou o drawdown/runup da posição.
    """
    state = _load_state()
    entries = state.get("entries", [])
    if not (0 <= idx < len(entries)):
        return None

    e = _migrate_entry(entries[idx])
    now = datetime.now().isoformat()

    risk = abs(float(e["entry"] or 0) - float(e["sl"] or 0)) if e["sl"] else None
    result_r = None
    if risk and risk > 0:
        gross_move = (float(exit_price) - float(e["entry"] or 0)) * int(e["side"] or 1)
        result_r = round(gross_move / risk, 3)

    e.update({
        "status": "closed",
        "exit_time": now,
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "pnl_cash": float(pnl_cash),
        "result_r": result_r,
        "mae_price": float(mae_price) if mae_price is not None else None,
        "mfe_price": float(mfe_price) if mfe_price is not None else None,
        "comment": comment or e.get("comment", ""),
    })

    entries[idx] = e
    state["entries"] = entries
    _save_state(state)
    return e


def update_mae_mfe(idx: int, current_price: float) -> None:
    """
    Atualiza MAE/MFE em tempo real para uma posição aberta.
    Chame a cada tick / candle durante a vida da posição.
    """
    state = _load_state()
    entries = state.get("entries", [])
    if not (0 <= idx < len(entries)):
        return

    e = _migrate_entry(entries[idx])
    if e.get("status") != "open":
        return

    entry = float(e.get("entry") or 0)
    side = int(e.get("side") or 1)
    move = (current_price - entry) * side  # positivo = a favor, negativo = contra

    # MAE: pior excursão contra (menor valor de move)
    current_mae = float(e["mae_price"]) if e.get("mae_price") is not None else 0.0
    e["mae_price"] = min(current_mae, move)

    # MFE: melhor excursão a favor (maior valor de move)
    current_mfe = float(e["mfe_price"]) if e.get("mfe_price") is not None else 0.0
    e["mfe_price"] = max(current_mfe, move)

    entries[idx] = e
    state["entries"] = entries
    _save_state(state)


def add_entry(
    symbol: str,
    side: int,
    entry_price: float,
    exit_price: float,
    size: float = 0.0,
    zone: str = ZONE_LIQUIDEZ,
    confirmation: str = "",
    followed_plan: bool = False,
    comment: str = "",
    ts: Optional[str] = None,
    # novos campos opcionais (retrocompatibilidade)
    sl: float = 0.0,
    tp: float = 0.0,
    risk_pct: float = 0.0,
    atr: float = 0.0,
    adx: float = 0.0,
    liquidity_level: float = 0.0,
    sweep_extreme: float = 0.0,
    spread_entry: float = 0.0,
    slippage_entry: float = 0.0,
    exit_reason: str = "manual",
    mae_price: Optional[float] = None,
    mfe_price: Optional[float] = None,
    pnl_cash: float = 0.0,
) -> dict:
    """
    Registra uma operação COMPLETA (entrada + saída já conhecidas).
    Compatível com o schema antigo — todos os novos campos são opcionais.
    """
    now = ts or datetime.now().isoformat()
    risk = abs(entry_price - sl) if sl else None
    result_r = None
    if risk and risk > 0:
        gross_move = (exit_price - entry_price) * side
        result_r = round(gross_move / risk, 3)

    state = _load_state()
    record = {
        "signal_time": now,
        "symbol": symbol,
        "side": int(side),
        "entry": float(entry_price),
        "sl": float(sl) if sl else None,
        "tp": float(tp) if tp else None,
        "volume": float(size),
        "risk_pct": float(risk_pct) if risk_pct else None,
        "atr": float(atr) if atr else None,
        "adx": float(adx) if adx else None,
        "liquidity_level": float(liquidity_level) if liquidity_level else None,
        "sweep_extreme": float(sweep_extreme) if sweep_extreme else None,
        "trigger_reason": confirmation,
        "spread_entry": float(spread_entry) if spread_entry else None,
        "slippage_entry": float(slippage_entry) if slippage_entry else None,
        "status": "closed",
        "exit_time": now,
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "pnl_cash": float(pnl_cash),
        "result_r": result_r,
        "mae_price": float(mae_price) if mae_price is not None else None,
        "mfe_price": float(mfe_price) if mfe_price is not None else None,
        # legado
        "followed_plan": bool(followed_plan),
        "zone": zone if zone in ZONES else ZONE_LIQUIDEZ,
        "comment": comment,
        "ts": now,
        # aliases legados
        "entry_price": float(entry_price),
        "confirmation": confirmation,
        "size": float(size),
    }
    state.setdefault("entries", []).append(record)
    _save_state(state)
    return record


def delete_entry(idx: int) -> bool:
    state = _load_state()
    entries = state.get("entries", [])
    if 0 <= idx < len(entries):
        entries.pop(idx)
        _save_state(state)
        return True
    return False


def clear() -> None:
    _save_state({"entries": []})


# ─────────────────────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────────────────────

def summary(state: dict) -> dict:
    """
    Métricas completas de consistência e edge quantitativo.

    Inclui as métricas do schema antigo (seguiu o plano, acerto por zona,
    expectativa em R) mais as novas análises baseadas em MAE/MFE/ADX/ATR.
    """
    entries = [_migrate_entry(e) for e in state.get("entries", [])]
    closed = [e for e in entries if e.get("status") != "open"]
    n = len(closed)

    out = {
        "n": n,
        "ex_followed": 0,
        "pct_followed": None,
        "wins": 0,
        "n_directional": 0,
        "pnl_total": 0.0,
        "exp_r": None,
        "by_zone": {},
        "equity": [],
        # novas métricas
        "avg_mae": None,
        "avg_mfe": None,
        "reached_1r": 0,
        "reached_2r": 0,
        "pct_reached_1r": None,
        "pct_reached_2r": None,
        "avg_result_r": None,
        "exit_reason_counts": {},
        "by_adx_bucket": {},
    }

    cumulative = 0.0
    result_rs = []
    mae_list = []
    mfe_list = []

    for e in closed:
        side = int(e.get("side") or 0)
        size = float(e.get("volume") or e.get("size") or 0.0)
        entry_p = float(e.get("entry") or e.get("entry_price") or 0)
        exit_p = float(e.get("exit_price") or entry_p)

        # PnL em R (preferência) ou em $ se disponível
        result_r = e.get("result_r")
        pnl_cash = e.get("pnl_cash")

        if result_r is not None:
            r_val = float(result_r)
        else:
            mv = (exit_p - entry_p) * side
            r_val = mv * size  # fallback: unidades × move (schema antigo)
            # tenta calcular R se tiver SL
            sl_p = e.get("sl")
            if sl_p and sl_p != entry_p:
                risk = abs(entry_p - float(sl_p))
                r_val = (mv / risk) if risk > 0 else 0.0

        out["n_directional"] += 1
        out["pnl_total"] += float(pnl_cash) if pnl_cash is not None else r_val
        cumulative += float(pnl_cash) if pnl_cash is not None else r_val

        if r_val > 0:
            out["wins"] += 1
        out["equity"].append({"ts": e.get("signal_time") or e.get("ts", ""), "equity": round(cumulative, 6)})

        if e.get("followed_plan"):
            out["ex_followed"] += 1

        z = e.get("zone", ZONE_LIQUIDEZ)
        bz = out["by_zone"].setdefault(z, {"n": 0, "wins": 0, "confirms": 0})
        bz["n"] += 1
        if r_val > 0:
            bz["wins"] += 1
        if e.get("trigger_reason") or e.get("confirmation"):
            bz["confirms"] += 1

        # ── Novas métricas ────────────────────────────────────────────────
        result_rs.append(r_val)

        mfe_raw = e.get("mfe_price")
        mae_raw = e.get("mae_price")
        sl_p = e.get("sl")
        risk = abs(entry_p - float(sl_p)) if sl_p else None

        if mfe_raw is not None:
            mfe_r = float(mfe_raw) / risk if risk and risk > 0 else float(mfe_raw)
            mfe_list.append(mfe_r)
            if mfe_r >= 1.0:
                out["reached_1r"] += 1
            if mfe_r >= 2.0:
                out["reached_2r"] += 1

        if mae_raw is not None:
            mae_r = abs(float(mae_raw)) / risk if risk and risk > 0 else abs(float(mae_raw))
            mae_list.append(mae_r)

        # Contagem por motivo de saída
        reason = e.get("exit_reason") or "unknown"
        out["exit_reason_counts"][reason] = out["exit_reason_counts"].get(reason, 0) + 1

        # Agrupamento por faixa de ADX
        adx_val = e.get("adx")
        if adx_val is not None:
            adx_val = float(adx_val)
            if adx_val < 20:
                bucket = "ADX<20"
            elif adx_val < 30:
                bucket = "ADX 20-30"
            elif adx_val < 40:
                bucket = "ADX 30-40"
            else:
                bucket = "ADX≥40"
            ab = out["by_adx_bucket"].setdefault(bucket, {"n": 0, "wins": 0, "sum_r": 0.0})
            ab["n"] += 1
            if r_val > 0:
                ab["wins"] += 1
            ab["sum_r"] += r_val

    # ── Agregações finais ─────────────────────────────────────────────────
    if n:
        out["pct_followed"] = round(out["ex_followed"] / n * 100, 1)

    for z, bz in out["by_zone"].items():
        bz["hit"] = round(bz["wins"] / bz["n"] * 100, 1) if bz["n"] else 0.0
        bz["lab"] = ZONE_LABEL.get(z, z)

    if out["n_directional"]:
        out["exp_r"] = round(sum(result_rs) / out["n_directional"], 4)
        out["avg_result_r"] = out["exp_r"]

    if mae_list:
        out["avg_mae"] = round(sum(mae_list) / len(mae_list), 3)
    if mfe_list:
        out["avg_mfe"] = round(sum(mfe_list) / len(mfe_list), 3)
    if n:
        out["pct_reached_1r"] = round(out["reached_1r"] / n * 100, 1)
        out["pct_reached_2r"] = round(out["reached_2r"] / n * 100, 1)

    for bucket, ab in out["by_adx_bucket"].items():
        ab["hit"] = round(ab["wins"] / ab["n"] * 100, 1) if ab["n"] else 0.0
        ab["exp_r"] = round(ab["sum_r"] / ab["n"], 4) if ab["n"] else 0.0

    return out


def add_batch(symbol="", side=0, prices=None, size=0.0,
              zone="", confirmation="", followed=True, comment="", ts=None):
    """Helper retrocompatível para registrar de uma vez (sessão única)."""
    return add_entry(symbol=symbol, side=side, entry_price=prices[0],
                     exit_price=prices[1], size=size, zone=zone,
                     confirmation=confirmation, followed_plan=followed,
                     comment=comment, ts=ts)