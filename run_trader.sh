#!/usr/bin/env bash
# Robô — executa sinais automaticamente no MetaTrader 5 a cada N minutos.
# Abre/fecha posições conforme o sinal, mesmo com o dashboard fechado.
# Para parar o robô: remova a linha do cron (ver README).
# Testar uma vez agora: ./run_trader.sh
# Forçar notificação sempre: ./run_trader.sh --force

set -uo pipefail
cd "$(dirname "$0")"

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE="--force"
fi

/usr/bin/python3 alerts.py \
    --check --execute \
    --symbol "XAU-USD" --interval "15m" --days 30 \
    --rsi-ob 70 --rsi-os 30 \
    --min-adx 20 \
    --trigger-mode pullback \
    --session "Londres + Nova York (melhor janela)" \
    --sl-atr 2.0 --tp-atr 3.0 --risk-pct 0.25 \
    $FORCE >> /tmp/trader.log 2>&1
