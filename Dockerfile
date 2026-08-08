FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Secrets vêm das variáveis de ambiente do deploy (entrypoint gera o toml).
RUN chmod +x entrypoint.sh

# Render injeta a porta via $PORT; o entrypoint.sh usa ${PORT:-8080}.
EXPOSE 8080

CMD ["sh", "entrypoint.sh"]