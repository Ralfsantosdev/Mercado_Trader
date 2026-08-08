#!/usr/bin/env bash
# Sobe o dashboard Forex (Streamlit) em segundo plano.
# Uso no boot (@reboot) ou manual: ./run_app.sh
set -uo pipefail
cd "$(dirname "$0")"

# Garante MT5 + bridge antes do app (opcional: ignore erros)
if [ -x ./start_mt5.sh ]; then
    ./start_mt5.sh >> /tmp/start_mt5_cron.log 2>&1 || true
fi

# Ativa no Market Watch os símbolos usados (senão XAUUSD não abre no gráfico)
sleep 8
/usr/bin/python3 ensure_symbols.py >> /tmp/start_mt5_cron.log 2>&1 || true

# Só sobe se ainda não estiver rodando
if pgrep -f "streamlit run app.py" > /dev/null; then
    echo "Streamlit já rodando." >> /tmp/app_cron.log
else
    nohup /home/ralf/.local/bin/streamlit run app.py \
        --server.address 127.0.0.1 --server.port 8501 \
        >> /tmp/app_cron.log 2>&1 &
    echo "Dashboard iniciado em http://127.0.0.1:8501" >> /tmp/app_cron.log
fi
