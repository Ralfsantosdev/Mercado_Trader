#!/usr/bin/env bash
# Sobe o terminal MetaTrader 5 (Wine) + o bridge RPyC (mt5server.exe).
# Uso: ./start_mt5.sh
# Depois disso, o dashboard pode executar sinais na conta logada do MT5.
# Para parar: ./start_mt5.sh stop
#
# Requisitos:
#   - MT5 instalado sob Wine (prefix ~/.mt5)
#   - conta logada no terminal (demo ou real)

set -uo pipefail

WINEPREFIX="$HOME/.mt5"
export WINEPREFIX DISPLAY="${DISPLAY:-:0}"
TERMINAL="$HOME/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe"
SERVER="$HOME/.local/share/mt5linux/mt5server.exe"
PORT=18812

case "${1:-start}" in
  stop)
    echo "Parando mt5server.exe e terminal64.exe..."
    pkill -f mt5server.exe 2>/dev/null
    pkill -f "MetaTrader 5/terminal64.exe" 2>/dev/null
    echo "OK."
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

    if pgrep -f mt5server.exe >/dev/null; then
      echo "✅ MT5 + bridge prontos. Abra o dashboard e escolha 'MetaTrader 5'."
    else
      echo "❌ Bridge não subiu. Veja /tmp/mt5server.log" >&2
    fi
    ;;
esac
