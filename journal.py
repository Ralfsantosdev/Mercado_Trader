"""
Diário de consistência — registra a QUALIDADE de execução, não só o PnL.

Filosofia (do usuário): na maioria o foco está no dinheiro/gráfico/indicadores;
o edge está em pensar e agir como o mercado — ler a estrutura de liquidez,
a zona neutra e as armadilhas, e só entrar na confirmação do movimento,
"como se fosse um produto na feira": o que transforma lotes em dinheiro é a
negociação, não a moeda.

Cada operação é guardada com a ZONA em que foi tomada (para validar a tese) e
se o PLANO foi seguido. As métricas-chave:

- CONSISTÊNCIA EXECUÇÃO: % dos trades que seguiram o próprio plano
  (independente de lucro). É a métrica principal — o lucro vem depois.
- Acerto por ZONA: para descobrir onde está o seu edge de leitura
  (liquidez/neutra/armadilha) e onde você está forçável.
- Expectativa por trade em R (o lucro médio honesto).

Persistência em JSON local. Estrutura pronta para exportar/analisar.
"""

import json
import os
from datetime import datetime
from typing import Optional

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_state.json")

ZONE_LIQUIDEZ = "liquidez"
ZONE_NEUTRA = "neutra"
ZONE_ARMADILHA = "armadilha"
ZONES = [ZONE_LIQUIDEZ, ZONE_NEUTRA, ZONE_ARMADILHA]
ZONE_LABEL = {
    ZONE_LIQUIDEZ: "🕳️ Zona de liquidez",
    ZONE_NEUTRA: "⚪ Zona neutra",
    ZONE_ARMADILHA: "🐍 Armadilha",
}


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"entries": []}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def add_entry(symbol: str, side: int, entry_price: float, exit_price: float,
              size: float = 0.0, zone: str = ZONE_LIQUIDEZ,
              confirmation: str = "", followed_plan: bool = False,
              comment: str = "", ts: Optional[str] = None) -> dict:
    """
    Registra uma operação. Retorna o dict salvo (com campos derivados).
    `followed_plan` é o que importa para consistência: você seguiu SUA regra?
    """
    state = _load_state()
    entry = {
        "symbol": symbol,
        "side": int(side),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "size": float(size),
        "zone": zone if zone in ZONES else ZONE_LIQUIDEZ,
        "confirmation": confirmation,
        "followed_plan": bool(followed_plan),
        "comment": comment,
        "ts": ts or datetime.now().isoformat(),
    }
    state.setdefault("entries", []).append(entry)
    _save_state(state)
    return entry


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


def summary(state: dict) -> dict:
    """Métricas de consistência (a base é o plano seguido, não o dinheiro)."""
    entries = state.get("entries", [])
    n = len(entries)
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
    }

    cumulative = 0.0
    for e in entries:
        side = e.get("side", 0)
        size = e.get("size", 0.0) or 0.0
        # R do plano: move no sentido da operação (independente de $)
        mv = (e.get("exit_price", 0) - e.get("entry_price", 0)) * side
        r = mv * size
        out["n_directional"] += 1
        out["pnl_total"] += r
        cumulative += r
        if r > 0:
            out["wins"] += 1
        out["equity"].append({"ts": e.get("ts", ""), "equity": round(cumulative, 6)})

        if e.get("followed_plan"):
            out["ex_followed"] += 1

        z = e.get("zone", ZONE_LIQUIDEZ)
        bz = out["by_zone"].setdefault(z, {"n": 0, "wins": 0, "confirms": 0})
        bz["n"] += 1
        if r > 0:
            bz["wins"] += 1
        if e.get("confirmation"):
            bz["confirms"] += 1

    if n:
        out["pct_followed"] = round(out["ex_followed"] / n * 100, 1)

    for z, bz in out["by_zone"].items():
        bz["hit"] = round(bz["wins"] / bz["n"] * 100, 1) if bz["n"] else 0.0
        bz["lab"] = ZONE_LABEL.get(z, z)

    if out["n_directional"]:
        out["exp_r"] = round(out["pnl_total"] / out["n_directional"], 4)

    return out


def add_batch(symbol="", side=0, prices=None, size=0.0,
              zone="", confirmation="", followed=True, comment="", ts=None):
    """Helper para registrar de uma vez (caso venha de uma sessão)."""
    return add_entry(symbol=symbol, side=side, entry_price=prices[0],
                     exit_price=prices[1], size=size, zone=zone,
                     confirmation=confirmation, followed_plan=followed,
                     comment=comment, ts=ts)