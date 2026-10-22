#!/usr/bin/env bash
# Sobe o terminal MetaTrader 5 (Wine) + o bridge RPyC (mt5server.exe).
# Uso:
#   ./start_mt5.sh            # sobe (idempotente)
#   ./start_mt5.sh restart    # derruba e sobe de novo (usado pelo watchdog)
#   ./start_mt5.sh status     # só verifica: porta + processo + login
#   ./start_mt5.sh stop       # para tudo
#
# Depois disso, o dashboard e o cron podem executar sinais na conta logada.
#
# Requisitos:
#   - MT5 instalado sob Wine (prefix ~/.mt5)
#   - conta logada no terminal (demo ou real)

set -uo pipefail

WINEPREFIX="$HOME/.mt5"
export WINEPREFIX
export DISPLAY="${DISPLAY:-:0}"
TERMINAL="$HOME/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe"
SERVER="$HOME/.local/share/mt5linux/mt5server.exe"
PORT=18812

port_open() {
  timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$PORT" 2>/dev/null
}

case "${1:-start}" in
  stop)
    echo "Parando mt5server.exe e terminal64.exe..."
    pkill -f mt5server.exe 2>/dev/null
    pkill -f "MetaTrader 5/terminal64.exe" 2>/dev/null
    echo "OK."
    ;;

  status)
    ok=0
    if pgrep -f terminal64.exe >/dev/null; then echo "terminal MT5: OK"; else echo "terminal MT5: PARADO"; ok=1; fi
    if pgrep -f mt5server.exe  >/dev/null; then echo "bridge:       OK"; else echo "bridge:       PARADO"; ok=1; fi
    if port_open;               then echo "porta $PORT:   OK"; else echo "porta $PORT:   FECHADA"; ok=1; fi
    # login/sessão — só tem sentido com a ponte no ar
    if port_open; then
      /usr/bin/python3 - "$PORT" <<'PY'
import sys
sys.path.insert(0, ".")
try:
    from mt5_bridge import get_bridge
    ok, why = get_bridge().ensure_ready()
    if ok:
        import mt5_bridge
        b = get_bridge()
        print(f"conta:        OK ({b.account_mode()})")
        print(f"offset fuso:  {b.utc_offset_hours()} h")
        sys.exit(0)
    print(f"conta:        FALHOU — {why}")
    sys.exit(1)
except Exception as e:
    print(f"conta:        FALHOU — {e}")
    sys.exit(1)
PY
      [ $? -eq 0 ] || ok=1
    fi
    exit $ok
    ;;

  restart)
    echo "Reiniciando MT5 + bridge..."
    pkill -f mt5server.exe 2>/dev/null
    pkill -f "MetaTrader 5/terminal64.exe" 2>/dev/null
    sleep 3
    exec "$0" start
    ;;

  start|*)
    if ! command -v wine >/dev/null 2>&1; then
      echo "ERRO: wine não encontrado." >&2; exit 1
    fi
    [ -f "$TERMINAL" ] || { echo "ERRO: terminal MT5 não encontrado em $TERMINAL" >&2; exit 1; }
    [ -f "$SERVER" ]  || { echo "ERRO: $SERVER não encontrado. Baixe de https://github.com/lucas-campagna/mt5linux/releases" >&2; exit 1; }

    if ! pgrep -f terminal64.exe >/dev/null; then
      echo "Abrindo MetaTrader 5..."
      nohup wine "$TERMINAL" >/tmp/mt5_terminal.log 2>&1 &
      sleep 10
    else
      echo "MetaTrader 5 já aberto."
    fi

    if pgrep -f mt5server.exe >/dev/null; then
      echo "Bridge já rodando na porta $PORT."
    else
      echo "Subindo bridge RPyC (mt5server.exe)..."
      nohup wine "$SERVER" >/tmp/mt5server.log 2>&1 &
      sleep 6
    fi

    # Health-check REAL: antes só se olhava o pgrep, que passa mesmo com a
    # porta fechada. Agora testa porta + initialize() + conta logada.
    if port_open; then
      echo "✅ MT5 + bridge prontos na porta $PORT."
      "$0" status
      exit $?
    else
      echo "❌ Bridge não subiu (porta $PORT fechada). Veja /tmp/mt5server.log" >&2
      exit 1
    fi
    ;;
esac
