#!/usr/bin/env python3
"""
Watchdog do robô — roda a cada minuto no cron e responde PERGUNTAS CRÍTICAS
que antes ninguém fazia:

  1. O bridge MT5 (porta 18812) está vivo? A conta está logada?
  2. O dashboard está rodando?
  3. O cron de trader está realmente executando (log fresco)?
  4. Tem posição aberta SEM SL/TP (posição nua)?  ← corta sozinha
  5. O kill-switch está ligado?

Falhou → tenta reiniciar o que caiu + notifica no Telegram (com cooldown).
Saída: 0 = tudo ok, 1 = alguma ação foi tomada (para o cron registrar).

Uso:
    python3 robot_watchdog.py                 # checa e conserta
    python3 robot_watchdog.py --check         # só checa, não reinicia
    python3 robot_watchdog.py --dry-run       # checa, não reinicia, não notifica
"""

import argparse
import os
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from alerts import notify_error, send_telegram, configured  # noqa: E402
from safety import kill_switch_reason, warn                  # noqa: E402

PORT = 18812
BRIDGE_LOG = "/tmp/watchdog.bridge.log"
APP_LOG = "/tmp/app_cron.log"
TRADER_LOG = "/tmp/trader.log"
HEARTBEAT_FILE = os.path.join(BASE, ".watchdog_state.json")
HEARTBEAT_PERIOD_S = 6 * 3600  # heartbeat pro Telegram a cada 6 h

RESTART_COOLDOWN_S = 15 * 60


def _age(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return float("inf")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return True
    except OSError:
        return False


def _run(cmd: list, log: str) -> int:
    try:
        with open(log, "a") as f:
            f.write(f"\n[watchdog {time.strftime('%F %T')}] {' '.join(cmd)}\n")
            f.flush()
            subprocess.run(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
                           timeout=300, check=False)
        return 0
    except Exception as e:
        warn(f"watchdog: falha ao rodar {cmd}: {e}")
        return 1


def _process_running(pattern: str) -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


def _cooldown_ok(key: str, cooldown: int) -> bool:
    import json
    state = {}
    try:
        with open(HEARTBEAT_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    last = float(state.get(key, 0) or 0)
    if time.time() - last < cooldown:
        return False
    state[key] = time.time()
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
    return True


def _notify(msg: str, key: str, cooldown: int = RESTART_COOLDOWN_S) -> None:
    if configured() and _cooldown_ok(key, cooldown):
        send_telegram(msg)


def check_naked_positions(m, problems: list) -> None:
    """Posição aberta com SL=0 e TP=0 = exposição sem proteção nenhuma."""
    try:
        for p in m.positions():
            if float(p.get("sl") or 0) <= 0 and float(p.get("tp") or 0) <= 0:
                problems.append(
                    f"⚠️ Posição NUA {p['symbol']} ticket {p['ticket']} "
                    f"({p['volume']} lot, entrada {p['entry']}) sem SL e sem TP."
                )
    except Exception as e:
        problems.append(f"não consegui listar posições: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="só verifica, não reinicia")
    ap.add_argument("--dry-run", action="store_true", help="não reinicia nem notifica")
    args = ap.parse_args()

    problems, actions = [], []

    # 0) kill-switch — informativo (não é falha, é decisão do usuário)
    ks = kill_switch_reason()
    if ks and _cooldown_ok("killswitch", 6 * 3600):
        _notify(f"🛑 Kill-switch ativo: {ks}", "killswitch", cooldown=0)

    # 1) bridge MT5
    if not _port_open(PORT):
        problems.append(f"bridge MT5 (porta {PORT}) fora do ar")
        if not args.check and not args.dry_run:
            if _cooldown_ok("bridge_restart", RESTART_COOLDOWN_S):
                if os.path.exists(os.path.join(BASE, "start_mt5.sh")):
                    _run(["bash", os.path.join(BASE, "start_mt5.sh"), "restart"], BRIDGE_LOG)
                    actions.append("reiniciando MT5+bridge")
            else:
                actions.append("reinício em cooldown")

    # 2) sessão/logged/conta
    try:
        from mt5_bridge import get_bridge
        m = get_bridge()
        ok, why = m.ensure_ready()
        if not ok:
            problems.append(f"MT5 não pronto: {why}")
        else:
            check_naked_positions(m, problems)
            mode = m.account_mode()
            if mode not in ("demo", "real", "contest"):
                problems.append(f"modo de conta inválido: {mode}")
    except Exception as e:
        problems.append(f"bridge import/erro: {e}")

    # 3) dashboard
    if not _process_running("streamlit run app.py"):
        problems.append("dashboard Streamlit parado")
        if not args.check and not args.dry_run:
            if _cooldown_ok("app_restart", RESTART_COOLDOWN_S) and \
               os.path.exists(os.path.join(BASE, "run_app.sh")):
                _run(["bash", os.path.join(BASE, "run_app.sh")], APP_LOG)
                actions.append("reiniciando dashboard")

    # 4) cron de trader realmente rodando? (log com no máximo 10 min)
    if _age(TRADER_LOG) > 600:
        problems.append(f"/tmp/trader.log parado há {int(_age(TRADER_LOG) / 60)} min "
                        "(cron de trader não está executando)")

    # reporta
    if problems:
        msg = "🚨 *WATCHDOG*\n" + "\n".join(f"• {p}" for p in problems)
        if actions:
            msg += "\n\n🔧 " + "\n".join(f"• {a}" for a in actions)
        print(msg.replace("*", ""), flush=True)
        if not args.dry_run:
            notify_error("\n".join(problems), cooldown_s=RESTART_COOLDOWN_S)
        return 1

    if _cooldown_ok("heartbeat", HEARTBEAT_PERIOD_S) and not args.dry_run:
        try:
            from mt5_bridge import get_bridge
            ai = get_bridge().account_info()
            send_telegram("💚 *WATCHDOG OK* — bridge, conta e cron de operação "
                          f"ativos. Equity {ai.get('equity')}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
