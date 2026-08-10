#!/usr/bin/env python3
"""Gera .streamlit/secrets.toml a partir das env vars do deploy.

Usa a lib `toml` (já instalada com o Streamlit) para escrever com escaping
correto — strings com aspas, $, barras, caracteres unicode etc. nunca quebram
o parse (antes um heredoc de shell deixava valor sem aspas e o Streamlit
crashava ao ler o arquivo).

Também é tolerante a nomes alternativos de env var e loga no boot do
container (Logs do Render) o que foi lido, para diagnóstico fácil.
"""
import os
import sys


def _first(*names):
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return ""


def main() -> int:
    try:
        import toml
    except ImportError:  # fallback mínimo (toml vem junto do streamlit)
        toml = None

    # Aceita os dois padrões de nome: o MAIÚSCULO (recomendado) e o minúsculo
    # (nome do secrets.toml). Muita gente cola no Render exatamente os nomes
    # que viu no secrets.toml.example (app_password, telegram_token, ...).
    app_password = _first("APP_PASSWORD", "ST_APP_PASSWORD", "app_password")
    aws_key = _first("AWESOMEAPI_API_KEY", "AWESOMEAPI_KEY", "awesomeapi_key")
    tg_token = _first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "telegram_token")
    tg_chat = _first("TELEGRAM_CHAT", "telegram_chat")

    secrets = {}
    if app_password:
        secrets["app_password"] = app_password
    if aws_key:
        secrets["awesomeapi_key"] = aws_key
    if tg_token and tg_chat:
        secrets["telegram_token"] = tg_token
        secrets["telegram_chat"] = tg_chat

    secrets_dir = ".streamlit"
    os.makedirs(secrets_dir, exist_ok=True)
    path = os.path.join(secrets_dir, "secrets.toml")

    # Não apaga config criada pelo usuário (ex.: rodando local).
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
        if "Gerado pelo entrypoint no boot do container" not in existing:
            print("=== secrets.toml local já configurado pelo usuário — não vou sobrescrever.")
            return 0

    # OBS: o marcador "# Gerado pelo entrypoint" TEM DE ESTAR no arquivo
    # (não só em comentário de docstring): é ele que permite que um boot
    # seguinte sobrescreva o toml quando novas env vars (ex.: Telegram) forem
    # adicionadas no deploy. Antes ele só era gravado no fallback sem a lib
    # `toml`, então o path normal gerava um arquivo "órfão" e o próximo boot
    # se recusava a atualizar — credenciais novas nunca entravam.
    with open(path, "w") as f:
        f.write("# Gerado pelo entrypoint no boot do container.\n")
        if toml is not None:
            toml.dump(secrets, f)
        else:
            # fallback sem lib — apenas valores simples, com aspas escapadas
            def esc(v: str) -> str:
                return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

            for k, v in secrets.items():
                f.write(f"{k} = {esc(v)}\n")

    print("=== Secrets gerados em", path)
    print("  app_password :", "OK" if app_password else "AUSENTE (APP_PASSWORD)")
    print("  awesomeapi   :", "OK" if aws_key else "AUSENTE")
    print("  telegram     :", "OK" if tg_token and tg_chat else "AUSENTE (opcional)")
    return 0


if __name__ == "__main__":
    sys.exit(main())