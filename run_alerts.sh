#!/usr/bin/env bash
# Alerta de sinal via Telegram — roda no cron a cada N minutos, sem o app aberto.
# Ajuste SYMBOL/INTERVAL para o par e timeframe que você acompanha no dashboard.
# Rodar manualmente: bash run_alerts.sh
# Testar notificação sempre (ignora o estado): bash run_alerts.sh --force

set -uo pipefail
cd "$(dirname "$0")"

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE="--force"
fi

/usr/bin/python3 alerts.py \
    --check \
    --symbol "XAU-USD" --interval "15m" --days 30 \
    --rsi-ob 70 --rsi-os 30 \
    --min-adx 20 \
    --trigger-mode pullback \
    --session "Londres + Nova York (melhor janela)" \
    $FORCE >> /tmp/alertas.log 2>&1
