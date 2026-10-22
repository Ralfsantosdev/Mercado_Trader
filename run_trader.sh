#!/usr/bin/env bash
# Robô — verifica o sinal e EXECUTA no MetaTrader 5 (cron a cada 5 min).
# Para parar o robô imediatamente: python safety.py disable "motivo"
# Testar agora:            ./run_trader.sh
# Forçar notificação:      ./run_trader.sh --force
#
# Segurança:
#   - flock no MESMO arquivo usado pelo Python (safety.execution_lock):
#     run_alerts.sh, run_trader.sh e o app disputam um único executor.
#   - kill-switch (TRADING_DISABLED) é checado dentro de alerts.py.
#   - rotação de log: sem ela /tmp/trader.log crescia sem limite e era inundado
#     por warnings do Streamlit.

set -uo pipefail
cd "$(dirname "$0")"

LOCKFILE="/tmp/mercado_trader.exec.lock"
LOG="/tmp/trader.log"
MAXLOG=$((2 * 1024 * 1024))   # 2 MB

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE="--force"
fi

# rotação simples
if [[ -f "$LOG" ]] && (( $(stat -c%s "$LOG" 2>/dev/null || echo 0) > MAXLOG )); then
    mv -f "$LOG" "${LOG}.1" 2>/dev/null || true
fi

# lock não-bloqueante: se outro executor está rodando, este tick pula fora
# (não enfileira ordem atrasada nem duplica).
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] skip: outro executor ativo" >> "$LOG"
    exit 0
fi

/usr/bin/python3 alerts.py \
    --check --execute \
    --symbol "XAU-USD" --interval "15m" --days 30 \
    --rsi-ob 70 --rsi-os 30 \
    --min-adx 20 \
    --trigger-mode pullback_liquidity \
    --session "Londres + Nova York (melhor janela)" \
    --sl-atr 2.0 --tp-atr 3.0 --risk-pct 0.25 \
    --max-open 1 \
    --max-spread-pips 60 \
    --min-tick-volume 1 \
    --source mt5 \
    $FORCE >> "$LOG" 2>&1
