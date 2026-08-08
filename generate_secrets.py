#!/usr/bin/env python3
"""Gera .streamlit/secrets.toml a partir das env vars do deploy.

Antes o entrypoint.sh fazia isso com shell/heredoc, que quebrava quando o
valor tinha aspas, $, barras etc. e não deixava claro o que foi lido.
Aqui: leitura tolerante a nomes alternativos, escaping TOML correto e um
log explícito no container (visível nos logs do Render) para diagnosticar.
"""
import json
import os
import sys


def _first(*names):
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return ""


def _esc(value: str) -> str:
    return json.dumps(value)  # TOML e JSON usam as mesmas regras de escapa básica


def main() -> int:
    app_password = _first("APP_PASSWORD", "ST_APP_PASSWORD")
    aws_key = _first("AWESOMEAPI_API_KEY", "AWESOMEAPI_KEY", "awesomeapi_key")
    tg_token = _first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
    tg_chat = _first("TELEGRAM_CHAT")

    secrets_dir = ".streamlit"
    os.makedirs(secrets_dir, exist_ok=True)
    path = os.path.join(secrets_dir, "secrets.toml")

    # Não apaga uma config existente criada pelo usuário (ex.: rodando local).
    # Só sobrescreve se o arquivo atual for geração de um entrypoint anterior
    # (marcado com o comentário abaixo) ou não existir.
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
        if "Gerado pelo entrypoint no boot do container" not in existing:
            print("=== secrets.toml local já configurado pelo usuário — não vou sobrescrever.")
            return 0

    lines = ["# Gerado pelo entrypoint no boot do container.", ""]
    lines.append(f"app_password = {_esc(app_password) if app_password else 'CHANGE_ME'}")
    lines.append("")
    if aws_key:
        lines.append(f"awesomeapi_key = {_esc(aws_key)}")
    else:
        lines.append("# awesomeapi_key NAO configurada -> usando cache (rate-limit)")

    if tg_token and tg_chat:
        lines.append("")
        lines.append(f"telegram_token = {_esc(tg_token)}")
        lines.append(f"telegram_chat = {_esc(tg_chat)}")

    with open(path, "w") as f:
        f.write("\n".join(lines))

    print("=== Secrets gerados em", path)
    print("  app_password :", "OK" if app_password else "AUSENTE (APP_PASSWORD)")
    print("  awesomeapi    :", "OK" if aws_key else "AUSENTE")
    print("  telegram      :", "OK" if tg_token and tg_chat else "AUSENTE (opcional)")
    return 0


if __name__ == "__main__":
    sys.exit(main())