#!/usr/bin/env bash
# Alerta de sinal via Telegram (cron a cada 5 min, sem o app aberto).
# Rodar manualmente: bash run_alerts.sh
# Testar sempre:     bash run_alerts.sh --force

set -uo pipefail
cd "$(dirname "$0")"

LOCKFILE="/tmp/mercado_trader.alerts.lock"
LOG="/tmp/alertas.log"
MAXLOG=$((2 * 1024 * 1024))

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE="--force"
fi

if [[ -f "$LOG" ]] && (( $(stat -c%s "$LOG" 2>/dev/null || echo 0) > MAXLOG )); then
    mv -f "$LOG" "${LOG}.1" 2>/dev/null || true
fi

# lock próprio do canal de alertas (independente do executor de ordens)
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    exit 0
fi

/usr/bin/python3 alerts.py \
    --check \
    --symbol "XAU-USD" --interval "15m" --days 30 \
    --rsi-ob 70 --rsi-os 30 \
    --min-adx 20 \
    --trigger-mode pullback_liquidity \
    --session "Londres + Nova York (melhor janela)" \
    --source mt5 \
    --smc-alerts \
    $FORCE >> "$LOG" 2>&1
