#!/bin/sh
# Gera .streamlit/secrets.toml a partir das env vars de deploy e sobe o app.
# O log abaixo aparece na aba Logs do Render — é o primeiro lugar a olhar
# quando o dashboard reclamar de senha/chave.

set -e

echo ">>> entrypoint: gerando secrets a partir das env vars"
python3 generate_secrets.py

echo ">>> entrypoint: subindo streamlit na porta ${PORT:-8080}"
exec streamlit run app.py --server.port "${PORT:-8080}"