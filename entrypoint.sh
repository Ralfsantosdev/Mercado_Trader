#!/bin/sh
# Converte variáveis de ambiente do Railway (AWSOMEAPI_KEY, APP_PASSWORD, etc.)
# em um secrets.toml que o Streamlit consegue ler. Roda antes do app.

set -e

SECRETS_DIR=".streamlit"
SECRETS_FILE="${SECRETS_DIR}/secrets.toml"
mkdir -p "${SECRETS_DIR}"

if [ ! -f "${SECRETS_FILE}" ]; then
    echo "# Gerado automaticamente a partir das env vars (Railway/Render).
# Arquivo é efêmero dentro do container; nunca vai para o repo." > "${SECRETS_FILE}"
fi

# app_password
if [ -n "${APP_PASSWORD:-}" ]; then
    (cat <<EOF
app_password = "$APP_PASSWORD"
EOF
) > "${SECRETS_FILE}"
fi

append() {
    local key="$1"
    shift
    for val in "$@"; do
        if [ -n "${val:-}" ]; then
            printf '\n%s = "%s"\n' "$key" "$val" >> "${SECRETS_FILE}"
        fi
    done
}

append_key() {
    local key="$1"
    local varval="${2:-}"
    if [ -n "$varval" ]; then
        printf '%s = "%s"\n' "$key" "$varval" >> "${SECRETS_FILE}"
    fi
}

append_key "awesomeapi_key" "${AWESOMEAPI_API_KEY}"
append_key "telegram_token" "${TELEGRAM_TOKEN}"
append_key "telegram_chat" "${TELEGRAM_CHAT}"

# Secret complexo (multilinea/objeto) via JSON opcional
if [ -n "${EXTRA_SECRETS_JSON:-}" ]; then
    echo "${EXTRA_SECRETS_JSON}" >> "${SECRETS_FILE}"
fi

echo "Secrets gravados em ${SECRETS_FILE}:"
grep -oE '^[a-z_]+' "${SECRETS_FILE}" | sed 's/.*/  - &/'

exec streamlit run app.py --server.port "${PORT:-8080}"