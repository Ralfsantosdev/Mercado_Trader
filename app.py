"""
Dashboard Forex — análise técnica + sinal por regras + ML + BACKTEST.

Correções nesta versão:
- Sinal por regras não morre mais no Volume zerado do Yahoo (forex):
  se o volume não existir, a confirmação usa ATR em alta.
- Thresholds de RSI configuráveis (antes fixos em 10/90, que nunca disparavam).
- Gatilho real de cruzamento MM9xMM21 (opcional).
- "Confiança" do ML agora é a MARGEM |p - 0.5| — antes era a probabilidade da
  classe vencedora, que é sempre >= 50% e ilude.
- BACKTEST incluído: spread, stop/take por ATR, risco % por trade, métricas
  honestas (expectativa, drawdown, Sharpe). SEM backtest você não sabe se o
  sinal paga as custas — agora sabe.
- Pipeline cacheado; atualização automática opcional.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import os as _os
import numpy as np

from data_provider import get_provider
from indicators import add_all_indicators
from model import (
    generate_rule_based_signals,
    apply_live_confirmation,
    volume_is_meaningful,
    train_ml_model,
    predict_latest,
    _is_in_session,
    signal_index,
)
from backtest import run_backtest, summarize
from executor import PaperBroker
from dashboard_ui import inject_css, header, trade_table_html

st.set_page_config(page_title="Forex Dashboard", page_icon="📈", layout="wide")


def get_secret(name: str, default: str = "") -> str:
    """Lê um secret de env var primeiro, depois do secrets.toml do Streamlit.

    Imune a falha de o entrypoint não ter gerado o arquivo (ex.: deploy):
    no Render as env vars são sempre injetadas e este helper resolve delas.
    """
    val = _os.environ.get(name, "")
    if val:
        return val
    try:
        return st.secrets.get(name, default) or default
    except Exception:
        return default

# Estilo visual (terminal de trading) — deve vir logo após set_page_config
inject_css()

# Zoom pela roda do mouse no gráfico (senão parece imagem estática) + barra de ferramentas
_PLOTLY_CONFIG = {"scrollZoom": True, "displayModeBar": True, "displaylogo": False}


def _fmt_price(value: float) -> str:
    """Formata preço conforme a ordem de grandeza (JPY: 2 casas; demais: 5)."""
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:.2f}"
    if value >= 10:
        return f"{value:.4f}"
    return f"{value:.5f}"


# ---------------------------------------------------------------------------
# Autenticação — desabilitada (ambiente local). Para reativar, descomente
# a chamada `check_password()` abaixo da definição. A senha fica em secrets.
# ---------------------------------------------------------------------------
def check_password() -> bool:
    if not get_secret("app_password"):
        st.warning(
            "⚠️ Nenhuma senha configurada em `.streamlit/secrets.toml` ou `APP_PASSWORD`. "
            "Rodando sem autenticação — configure antes de expor este app publicamente."
        )
        return True

    pwd = st.sidebar.text_input("Senha", type="password")
    if pwd != get_secret("app_password"):
        if pwd:
            st.sidebar.error("Senha incorreta.")
        st.stop()
    return True


# AUTENTICAÇÃO ATIVADA — o app exige senha antes de abrir o dashboard.
# Senha vem de `.streamlit/secrets.toml` → app_password (definida via APP_PASSWORD
# no deploy Render, ou no secrets.toml local).
check_password()

# ---------------------------------------------------------------------------
# Sidebar — configurações
# ---------------------------------------------------------------------------
st.sidebar.title("⚙️ Configurações")
st.sidebar.caption("Parâmetros do terminal")

source = st.sidebar.selectbox(
    "Fonte de dados",
    ["awesomeapi", "mt5", "yfinance"],
    index=1,
    help="awesomeapi: cotação tempo real. mt5: puxa do MetaTrader 5 via mt5server.exe. yfinance: Yahoo Finance.",
)

_awesome_symbols = [
    "XAU-USD", "EUR-USD", "GBP-USD", "USD-JPY", "AUD-USD", "USD-CAD", "NZD-USD", "USD-CHF",
    "EUR-GBP", "EUR-JPY", "USD-BRL", "EUR-BRL", "BTC-BRL", "ETH-BRL", "DOGE-BRL",
    "US500", "NAS100", "US30", "GER40", "IBOV",
]
_yfinance_symbols = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]

symbol = st.sidebar.selectbox(
    "Par de moedas",
    _awesome_symbols if source in ("awesomeapi", "mt5") else _yfinance_symbols,
    format_func=lambda x: x.replace("=X", ""),
)

interval = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "2m", "5m", "15m", "30m", "1h", "1d"],
    index=3,
    help="awesomeapi fornece ticks de ~1 min e o app remonta candles no timeframe "
         "pedido (dados reais, diferente do Yahoo que não entrega forex intradiário). "
         "Para 1d usa o endpoint de fechamento diário.",
)

# Limites de histórico por fonte/timeframe
if source == "awesomeapi":
    # intraday: montado de ticks de 1 min via paginação (~30 dias); diário: até 360 dias
    _max_days = {"1m": 1, "2m": 2, "5m": 30, "15m": 30, "30m": 30, "1h": 30, "1d": 360}[interval]
elif source == "mt5":
    # MT5 history is typically extensive depending on broker
    _max_days = {"1m": 30, "2m": 30, "5m": 90, "15m": 90, "30m": 90, "1h": 365, "1d": 1000}[interval]
else:
    _max_days = {"1m": 7, "2m": 14, "5m": 60, "15m": 60, "30m": 45, "1h": 365, "1d": 365}[interval]

# Streamlit slider requires min_value < max_value
_max_days = max(2, _max_days)
days = st.sidebar.slider("Período (dias de histórico)", 1, _max_days, min(30, _max_days))

st.sidebar.subheader("🎯 Estratégia Principal")
trigger_mode = st.sidebar.selectbox(
    "Filosofia de Operação (Gatilho)",
    ["pullback_liquidity", "fimathe_tokio", "3_9_20", "cross", "pullback", "auto"],
    index=0,
    help="pullback_liquidity = (Recomendado) Sweep SMC + rejeição. "
         "fimathe_tokio = Rompimento Tóquio. "
         "Outros = Legado."
)

# Valores padrão invisíveis para manter compatibilidade
rsi_ob, rsi_os, use_cross = 70, 30, False
pullback_ema = 50
fimathe_hour, fimathe_candles, fimathe_max_pips = 20, 4, 30

if trigger_mode in ["auto", "slope", "cross"]:
    st.sidebar.caption("Parâmetros de Cruzamento / RSI")
    rsi_ob = st.sidebar.slider("RSI sobrecompra (limite)", 60, 95, 70)
    rsi_os = st.sidebar.slider("RSI sobrevenda (limite)", 5, 40, 30)
    use_cross = st.sidebar.checkbox("Gatilho por cruz. MM9×MM21", value=True)
elif trigger_mode == "pullback":
    st.sidebar.caption("Parâmetros de Pullback")
    pullback_ema = st.sidebar.slider("EMA do pullback", 13, 100, 50, step=1)
elif trigger_mode == "fimathe_tokio":
    st.sidebar.caption("⛩️ Parâmetros Fimathe")
    fimathe_hour = st.sidebar.number_input("Hora de abertura (0-23)", 0, 23, 20)
    fimathe_candles = st.sidebar.number_input("Duração (candles)", 1, 20, 4)
    fimathe_max_pips = st.sidebar.number_input("Max Tamanho (pips)", 10, 2000, 30)
elif trigger_mode == "pullback_liquidity":
    st.sidebar.caption("🧲 Institucional (SMC) ativo. Gatilhos fixos e matemáticos baseados em liquidez.")

use_volume = st.sidebar.checkbox("Confirmar com volume", value=True, help="Usa ATR se não houver volume.")

st.sidebar.subheader("🌍 Condição de mercado (filtros)")
_session_names = ["", "Sessão de Tóquio", "Sessão de Londres", "Sessão de Nova York",
                  "Londres + Nova York (melhor janela)", "Tóquio + Londres", "Cripto 24h"]
session = st.sidebar.selectbox(
    "Só operar na sessão",
    _session_names,
    index=4,
    format_func=lambda x: x if x else "— todas as horas (desligado)",
    help="Filtra os sinais para horários de alta liquidez (UTC). Fora deles o spread é "
         "maior e os movimentos têm menos continuidade.",
)
min_adx = st.sidebar.slider("ADX mínimo (força da tendência)", 0, 50, 20, step=5,
                            help="0 = desligado. ADX > 20-25 indica tendência aproveitável; "
                                 "abaixo disso o mercado está lateral (range) e o sinal é ignorado.")

st.sidebar.subheader("🌋 Volatilidade (regime + stop adaptativo)")
vol_max_pct = st.sidebar.number_input(
    "Bloquear vol extrema (percentil)", min_value=50, max_value=100, value=90, step=5,
    help="Percentil rolante da volatilidade precisa (Parkinson/Garman-Klass/Yang-Zhang) "
         "na própria história do ativo. Acima disso = rajada/notícia: whipsaw, o stop morre "
         "antes do preço ir pro alvo. 100 = desligado. Funciona em qualquer par/timeframe "
         "sem calibrar (é relativo, não fixo em pips).")
vol_adaptive_stop = st.sidebar.checkbox(
    "Stop adaptativo à volatilidade", value=True,
    help="Multiplica SL/TP pelo regime: percentil ≤50 = ×1.0 (nada muda), até ×1.6 em P100. "
         "Só ALARGA o stop em rajada — nunca aperta.")
vol_er_gate = st.sidebar.checkbox(
    "Bloquear lateral pura (chop)", value=True,
    help="Efficiency Ratio de Kaufman < 0.15 = mercado sem direção (varre os dois lados). "
         "Nesse regime stop é atingido por ruído, não por estrutura.")

st.sidebar.subheader("💰 Backtest")
_is_gold = symbol.startswith("XAU")
_is_index = symbol in ("US500", "NAS100", "US30", "GER40", "IBOV")
if _is_gold:
    _pip_size = 0.01
    _default_spread = 25.0
elif _is_index:
    _pip_size = 1.0  # Índices: 1 ponto = 1 unidade
    _default_spread = 5.0
else:
    _pip_size = 0.0001
    _default_spread = 2.0
spread_pips = st.sidebar.number_input("Spread (pips)", min_value=0.0, max_value=50.0, value=_default_spread, step=0.5,
                                      help=f"Custo por operação (ida+volta). {symbol}: 1 pip = {_pip_size}. "
                                           "Sem ele o backtest mente.")
sl_atr = st.sidebar.number_input("Stop loss (×ATR)", min_value=0.5, max_value=10.0, value=2.0, step=0.5)

tp_mode = st.sidebar.radio("Modo Take Profit", ["Pips", "ATR"], index=0, horizontal=True)
if tp_mode == "Pips":
    tp_pips = st.sidebar.number_input("Take profit (pips)", min_value=10.0, max_value=1000.0, value=100.0, step=10.0)
    tp_atr = 0.0
else:
    tp_pips = 0.0
    tp_atr = st.sidebar.number_input("Take profit (×ATR)", min_value=0.5, max_value=10.0, value=3.0, step=0.5)

ts_enabled = st.sidebar.checkbox("Ativar Trailing Stop", value=False)
if ts_enabled:
    trailing_stop_pips = st.sidebar.number_input("Distância do Trailing Stop (pips)", min_value=10.0, max_value=500.0, value=50.0, step=10.0)
else:
    trailing_stop_pips = 0.0

swap_pips_per_day = st.sidebar.number_input("Custo Swap/Overnight (pips/dia)", min_value=0.0, max_value=50.0, value=0.5, step=0.1,
                                            help="Custo deduzido por dia se a posição for mantida overnight.")

risk_pct = st.sidebar.number_input("Risco por trade (%)", min_value=0.1, max_value=10.0, value=0.25, step=0.25,
                                   help="Dimensionamento: o SL custa exatamente esse % da equity. "
                                        "0,25% é conservador; 1% em forex com spread já é agressivo.")

st.sidebar.subheader("🤖 Análise de probabilidade e risco (IA)")
llm_enabled = st.sidebar.checkbox(
    "Ligar agente LLM (gasta tokens)",
    value=False,
    help="Habilita a Inteligência Artificial gerativa avançada. Requer chave de API e consome tokens."
)
llm_provider = st.sidebar.selectbox("Provedor LLM", ["gemini", "openai"], index=0)
llm_api_key = st.sidebar.text_input(
    f"Chave da API ({llm_provider.capitalize()})",
    type="password",
    help="Opcional se definida no secrets.toml."
)
_secret_key = get_secret(f"{llm_provider.upper()}_API_KEY") or get_secret("llm_api_key")
if llm_enabled and not llm_api_key and not _secret_key:
    st.sidebar.warning(f"⚠️ Insira a chave da API para usar o {llm_provider.capitalize()} ou defina no secrets.toml.")

llm_config = None
if llm_enabled:
    llm_config = {"api_key": llm_api_key or _secret_key, "provider": llm_provider}

ai_enabled = st.sidebar.checkbox(
    "Ativar estatística local (Monte Carlo/IA Preditiva)",
    value=False,
    help="Estatística local (Monte Carlo, VaR, P(TP/SL)) — somente offline, "
         "sem chamadas de API e sem custo de tokens.",
)
use_dynamic_risk = st.sidebar.checkbox(
    "Gestão de Risco Dinâmica (Kelly Fracionado via IA)",
    value=True,
    help="Ajusta o % de risco de cada trade com base na 'Margem de Confiança' do "
         "modelo de Machine Learning. Oportunidades claras recebem mais lote."
)
ai_horizon = st.sidebar.slider("Horizonte da simulação (candles)", 5, 100, 30, step=5,
                               help="Quantos candles à frente o Monte Carlo simula.")
ai_paths = st.sidebar.slider("Caminhos simulados (Monte Carlo)", 500, 5000, 2000, step=500,
                             help="Mais caminhos = estimativa mais estável (um pouco mais lento).")

st.sidebar.subheader("💸 Execução")
exec_enabled = st.sidebar.checkbox(
    "Acompanhar sinais (paper/MT5)",
    value=False,
    help="Executa os sinais no papel (preço real, slippage) OU no MetaTrader 5 "
         "real/demo, conforme o broker escolhido.",
)
broker_type = st.sidebar.selectbox(
    "Broker",
    ["Paper (demo)", "MetaTrader 5"],
    index=0,
    help="Paper: simulado, persiste em execution_state.json. MT5: executa na conta "
         "logada no terminal (requer mt5server.exe rodando — ver README).",
)
slippage_pips = st.sidebar.number_input("Slippage simulado (pips)", min_value=0.0, max_value=20.0,
                                        value=1.0, step=0.5,
                                        help="Degradação realista de execução por ordem (só paper).")
max_open_positions = st.sidebar.number_input("Max posições simultâneas (Portfólio)", min_value=1, max_value=20, value=1, step=1,
                                             help="Gestão de portfólio: bloqueia novos sinais se o limite de posições estiver atingido, reduzindo exposição cruzada.")
exec_capital = st.sidebar.number_input("Capital inicial (demo)", min_value=100.0, max_value=1_000_000.0,
                                       value=10000.0, step=500.0)
if st.sidebar.button("🗑️ Zerar conta"):
    st.session_state["_reset_exec"] = True

st.sidebar.subheader("🛡️ Confirmação ao vivo (MT5)")
mt5_filter = st.sidebar.checkbox(
    "Confirmar com spread/tick ao vivo",
    value=True,
    help="Usa o que o terminal MT5 oferece de verdade: bloqueia a entrada quando o "
         "spread real (ask-bid) estiver alto ou o volume de tick for baixo. "
         "Requer mt5server.exe rodando. Sem ele, o filtro fica desligado.",
)
# 1 pip do ouro = 0.01; o default de forex (5p = 0.05) bloqueava Toda entrada
# em XAU-USD porque o spread real é ~0.23 (= 23p). 60p = 0.60 de teto.
_default_max_spread = 60.0 if _is_gold else 5.0
max_spread = st.sidebar.number_input("Spread máximo ao vivo (pips)", min_value=0.5, max_value=500.0,
                                     value=_default_max_spread, step=0.5,
                                     help="Acima disso a entrada é bloqueada (economia paga o stop, "
                                          "ex.: em horário de notícia). 0 desliga o spread.")
min_tick_vol = st.sidebar.number_input("Volume de tick mínimo", min_value=0, max_value=1000,
                                       value=0, step=1,
                                       help="Entradas são bloqueadas quando o candle atual tem menos "
                                            "que isso de volume de tick real. 0 desliga.")

if trigger_mode == "pullback_liquidity":
    smc_alerts_enabled = True
else:
    smc_alerts_enabled = st.sidebar.checkbox("🧲 Mostrar zonas SMC (FVG/Liquidez)", value=False)

st.sidebar.subheader("🚀 Robô Autônomo (Background)")
st.sidebar.caption("Inicia a execução institucional em 2º plano. Ele usa os parâmetros exatos definidos acima.")

import subprocess
import os
import signal

pid_file = "robot.pid"
robot_running = False
if os.path.exists(pid_file):
    try:
        with open(pid_file, "r") as f:
            robot_pid = int(f.read().strip())
        os.kill(robot_pid, 0) # verifica se o processo existe
        robot_running = True
    except (OSError, ValueError):
        os.remove(pid_file)

if robot_running:
    st.sidebar.success("🟢 Robô está RODANDO (ver /tmp/trader_bg.log)")
    if st.sidebar.button("⏹️ Parar Robô", use_container_width=True):
        try:
            with open(pid_file, "r") as f:
                robot_pid = int(f.read().strip())
            os.kill(robot_pid, signal.SIGTERM)
            os.remove(pid_file)
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Erro: {e}")
else:
    st.sidebar.info("⚪ Robô está PARADO")
    if st.sidebar.button("▶️ Ligar Robô", type="primary", use_container_width=True):
        cmd = [
            "python3", "alerts.py", "--check", "--execute",
            "--symbol", symbol, "--interval", interval, "--days", str(days),
            "--rsi-ob", str(rsi_ob), "--rsi-os", str(rsi_os),
            "--trigger-mode", trigger_mode, "--pullback-ema", str(pullback_ema),
            "--min-adx", str(min_adx), "--sl-atr", str(sl_atr), "--tp-atr", str(tp_atr),
            "--risk-pct", str(risk_pct), "--pip-size", str(_pip_size),
            "--vol-max-pct", str(vol_max_pct),
            "--vol-er-min", "0.15" if vol_er_gate else "0",
            "--fimathe-hour", str(int(fimathe_hour)), "--fimathe-candles", str(int(fimathe_candles)),
            "--fimathe-max-pips", str(fimathe_max_pips),
            "--source", source, "--continuous",
            "--session", session,
            "--max-open", str(int(max_open_positions)),
            "--max-spread-pips", str(max_spread),
            "--min-tick-volume", str(min_tick_vol),
            "--tp-pips", str(tp_pips),
            "--trailing-stop-pips", str(trailing_stop_pips),
        ]
        if not vol_adaptive_stop:
            cmd.append("--no-vol-adaptive")
        if not use_cross:
            cmd.append("--no-cross")
        if not use_volume:
            cmd.append("--no-volume")
        if smc_alerts_enabled:
            cmd.append("--smc-alerts")
        cmd.extend(["--session", session])
        
        try:
            with open("/tmp/trader_bg.log", "a") as logfile:
                p = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, preexec_fn=os.setpgrp)
            with open(pid_file, "w") as f:
                f.write(str(p.pid))
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Erro ao iniciar: {e}")

st.sidebar.subheader("📣 Alertas Telegram")
import alerts as alerts_mod
alerts_enabled = st.sidebar.checkbox("Notificar mudança de sinal no Telegram", value=False,
                                     help="Envia mensagem quando o sinal muda (neutro→compra, etc.). "
                                          "Configura telegram_token e telegram_chat no secrets.toml.")
if st.sidebar.button("📤 Enviar alerta de teste"):
    if not alerts_mod.configured():
        st.sidebar.error("Configure telegram_token e telegram_chat no .streamlit/secrets.toml primeiro.")
    else:
        st.sidebar.success(alerts_mod.send_test_alert())
if not alerts_mod.configured():
    st.sidebar.caption("⚠️ Sem credenciais do Telegram. Veja README.")

_refresh_opts = {"1s": 1, "5s": 5, "10s": 10, "30s": 30, "1min": 60, "2min": 120, "5min": 300}
refresh_label = st.sidebar.selectbox("Atualizar automaticamente", list(_refresh_opts.keys()), index=2)
auto_refresh = st.sidebar.checkbox(f"Ativado (a cada {refresh_label})", value=True)

if st.sidebar.button("🔄 Atualizar dados agora"):
    st.cache_data.clear()

st.sidebar.subheader("🎨 Visual do Gráfico")
chart_type = st.sidebar.radio("Tipo de Gráfico", ["Candles", "Barras (OHLC)"], index=0, horizontal=True)
show_smart_cloud = st.sidebar.checkbox("☁️ SmartCloudTrend (EMA 20x50)", value=False)
show_market_state = st.sidebar.checkbox("🎯 Estado do Mercado (Equilíbrio/Desequilíbrio)", value=True,
                                         help="Mostra fundo colorido: verde=equilíbrio, vermelho=desequilíbrio, amarelo=transição")

if trigger_mode in ["auto", "cross", "slope"]:
    show_bb = st.sidebar.checkbox("📉 Bandas de Bollinger", value=True)
else:
    show_bb = False

vol_profile_on = st.sidebar.checkbox("📊 Perfil de Volume (Profit Pro)", value=(trigger_mode == "pullback_liquidity"))

st.sidebar.caption(
    "Fonte: **AwesomeAPI** em tempo real (chave em `.streamlit/secrets.toml`) "
    "ou **Yahoo Finance (yfinance)**. Troca em 1 linha em `data_provider.py`."
)


# ---------------------------------------------------------------------------
# Pipeline de dados (cacheado) e ML (cacheado) — definidos fora do fragment
# Removido cache duplo aqui para permitir MT5 em tempo real (AwesomeAPI já tem cache interno)
def load_and_prepare(source, symbol, days, interval, rsi_ob, rsi_os, use_cross, trigger_mode, pullback_ema,
                     session, min_adx, max_spread_pips, min_tick_volume, fimathe_hour, fimathe_candles, fimathe_max_pips,
                     vol_max_pct=90, vol_er_gate=True):
    import streamlit as st
    api_key = get_secret("awesomeapi_key") if source == "awesomeapi" else None
    provider = get_provider(source=source, api_key=api_key)
    try:
        df = provider.get_ohlcv(symbol, days=days, interval=interval)
    except Exception as e:
        # erro de API/chave → deixa o app mostrar a mensagem, sem dado para trabalhar
        st.session_state["_data_error"] = str(e)
        return pd.DataFrame()
    st.session_state["_data_error"] = ""
    if df.empty:
        return df

    df["Return"] = df["Close"].pct_change()
    df["Direction"] = (df["Return"].shift(-1) > 0).astype(int)
    df = add_all_indicators(df)
    _er_min = 0.15 if vol_er_gate else 0.0
    df = generate_rule_based_signals(
        df,
        rsi_overbought=rsi_ob,
        rsi_oversold=rsi_os,
        use_volume=use_volume,
        use_cross=use_cross,
        trigger_mode=trigger_mode,
        pullback_ema=pullback_ema,
        session=session,
        min_adx=float(min_adx),
        pip_size=_pip_size,
        fimathe_hour=fimathe_hour,
        fimathe_candles=fimathe_candles,
        fimathe_max_pips=fimathe_max_pips,
        max_vol_percentile=float(vol_max_pct),
        min_efficiency_ratio=_er_min,
        pls_config_kwargs={
            "max_vol_percentile": float(vol_max_pct),
            "min_efficiency_ratio": _er_min,
            "adaptive_stop": bool(vol_adaptive_stop),
        },
    )
    return df


@st.cache_data(ttl=120, show_spinner=False)
def train_cached(df):
    try:
        return train_ml_model(df)
    except ValueError as e:
        return None, {"error": str(e)}, None


@st.cache_data(ttl=60, show_spinner="Simulando Monte Carlo...")
def ai_analysis_cached(df, direction, sl_atr, tp_atr, spread_pips, pip_size,
                       ml_p, ml_pred, horizon, n_paths, llm_config=None):
    import ai_analysis
    return ai_analysis.run_analysis(
        df, direction, sl_atr=sl_atr, tp_atr=tp_atr, spread_pips=spread_pips,
        pip_size=pip_size, ml_p=ml_p, ml_pred=ml_pred,
        horizon=horizon, n_paths=n_paths, llm_config=llm_config,
    )


# ---------------------------------------------------------------------------
# Corpo do dashboard — envolto em fragment p/ auto-refresh (60s)
# ---------------------------------------------------------------------------
def _mt5_live_quote(symbol):
    """Busca quote real do MT5 (spread/tick volume). None se bridge indisponível."""
    try:
        from mt5_bridge import get_bridge
        m = get_bridge()
        if not m.available():
            return None
        m.initialize()
        q = m.quote(symbol.replace("-", ""))
        return q if q.get("bid", 0) else None
    except Exception:
        return None


def render_main():
    df = load_and_prepare(source, symbol, days, interval, rsi_ob, rsi_os, use_cross, trigger_mode, pullback_ema,
                          session, min_adx, float(spread_pips * 2), 5,
                          int(fimathe_hour), int(fimathe_candles), float(fimathe_max_pips),
                          vol_max_pct=int(vol_max_pct), vol_er_gate=bool(vol_er_gate))

    if df.empty:
        _prev_err = st.session_state.get("_data_error", "")
        if _prev_err:
            st.error(f"❌ Falha ao buscar dados da AwesomeAPI: {_prev_err}")
            st.stop()
        st.error(
            "Nenhum dado retornado. "
            + ("A AwesomeAPI está sem limite/rate-limit (provavelmente falou a "
               "`AWESOMEAPI_API_KEY` no deploy — sem ela o acesso é limitado por IP, "
               "e o IP do data center é mais restrito). Defina a chave na aba "
               "Environment do Render e redeploy. "
               if source == "awesomeapi"
               else "Isso é comum com yfinance para forex intradiário — "
                    "tente aumentar o período, mudar para timeframe '1d', "
                    "ou trocar a fonte de dados.")
        )
        st.stop()

    header("📈 Forex Dashboard", "Análise técnica · Machine Learning · Backtest · Execução")

    # -------------------------------------- Confirmação ao vivo (spread/tick MT5)
    # Um único quote do MT5 por re-render — reutilizado no filtro, na cotação
    # ao vivo e no candle em formação do gráfico (update a cada 1s).
    live_q = _mt5_live_quote(symbol)
    mt5_block_msg = ""
    if mt5_filter:
        if live_q:
            df = apply_live_confirmation(
                df, live_q,
                max_spread_pips=float(max_spread),
                min_tick_volume=float(min_tick_vol),
                pip_size=_pip_size,
            )
            mt5_block_msg = df.attrs.get("signal_block_msg", "") or ""
            if mt5_block_msg:
                st.warning(f"🛡️ Entrada bloqueada ao vivo: {mt5_block_msg}")
        else:
            st.caption("⚠️ Filtro MT5 pedido, mas o bridge não está rodando — confirmação ao vivo desligada.")

    # Bloqueio por regime de volatilidade (percentil extremo / chop)
    _vol_block_msg = df.attrs.get("vol_block_msg", "") or ""
    if _vol_block_msg:
        st.warning(f"🌋 Entrada bloqueada pela volatilidade: {_vol_block_msg}")

    # ------------------------------------------------------------ Cotação ao vivo
    if source == "mt5":
        # MT5 ao vivo: bid/ask direto do terminal (broker), sem AwesomeAPI
        try:
            from dashboard_ui import quote_card
            if live_q is None or not live_q.get("bid"):
                st.caption("⚠️ Cotação ao vivo do MT5 indisponível — confira se o mt5server.exe está rodando.")
            else:
                bid = float(live_q["bid"])
                ask = float(live_q["ask"])
                spread = float(live_q.get("spread", 0.0))
                digits = int(live_q.get("digits", 0) or 0)
                spread_fmt = f"{spread:.{digits}f}" if spread > 0 else "—"
                cards = "".join([
                    quote_card("BID (compra)", _fmt_price(bid)),
                    quote_card("ASK (venda)", _fmt_price(ask)),
                    quote_card("SPREAD", spread_fmt),
                    quote_card("TICKS", f"{int(live_q.get('tick_volume', 0))}"),
                ])
                st.markdown(f"<div class='quote-strip'>{cards}</div>", unsafe_allow_html=True)
                st.caption(f"🔴 Fonte: **MetaTrader 5** ao vivo — {live_q['symbol']} (digits {digits}).")
        except Exception as e:
            st.caption(f"⚠️ Não foi possível obter a cotação ao vivo: {e}")

        # ------------------------- Candle em formação + decisão (manual, sem execução)
        if len(df):
            try:
                from dashboard_ui import quote_card
                last = df.iloc[-1]
                o = float(last["Open"]); h = float(last["High"])
                l = float(last["Low"]); c = float(last["Close"])
                body_pct = (c - o) / o * 100 if o else 0.0
                bar_start = last.name
                total = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
                         "1h": 3600, "1d": 86400}.get(interval, 300)
                # os candles do MT5 vêm no horário do SERVIDOR da corretora (ex.: UTC+2).
                # O progresso usa o horário do último tick (também do servidor) p/ ficar
                # consistente — sem chutar fuso.
                server_now = None
                if live_q and live_q.get("time"):
                    server_now = pd.Timestamp(int(live_q["time"]), unit="s", tz="UTC")
                if server_now is not None:
                    elapsed = max(0.0, (server_now - bar_start).total_seconds())
                    frac = min(elapsed / total, 1.0)
                    rem = max(0, int(total - elapsed))
                else:
                    frac = None
                    rem = None

                status = "—"
                live_spread_pips = 0.0
                if live_q and live_q.get("bid"):
                    live_spread_pips = float(live_q.get("spread") or 0.0) / _pip_size if _pip_size else 0.0
                    probe = apply_live_confirmation(
                        df.iloc[[-1]].copy(), live_q,
                        max_spread_pips=float(max_spread),
                        min_tick_volume=float(min_tick_vol),
                        pip_size=_pip_size,
                    )
                    block = probe.attrs.get("signal_block_msg", "") or ""
                    status = "🟢 ok p/ entrar" if not block else f"⛔ {block}"

                sig = int(df["Signal"].iloc[signal_index(df, trigger_mode)])
                sig_icon = {1: "🟢", -1: "🔴", 0: "⚪"}[sig]
                sig_lab = {1: "COMPRA", -1: "VENDA", 0: "NEUTRO"}[sig]
                sess_ok = True
                if server_now is not None:
                    sess_ok = bool(_is_in_session(pd.DatetimeIndex([server_now]), session).iloc[0])

                candle_cards = "".join([
                    quote_card("CANDLE O", _fmt_price(o)),
                    quote_card("CANDLE H", _fmt_price(h)),
                    quote_card("CANDLE L", _fmt_price(l)),
                    quote_card("CANDLE C", _fmt_price(c),
                               f"{body_pct:+.3f}%", 1 if body_pct > 0 else (-1 if body_pct < 0 else 0)),
                ])
                st.markdown(f"<div class='quote-strip'>{candle_cards}</div>", unsafe_allow_html=True)
                if frac is not None:
                    st.progress(frac)
                st.caption(
                    f"🕯️ Candle {interval} em formação"
                    + (f" — {frac * 100:.0f}% decorrido ({rem // 60}m {rem % 60:02d}s restantes)"
                       if frac is not None else " (progresso indisponível)")
                    + f" · abre {bar_start.strftime('%H:%M')} (hora do servidor)"
                )
                st.markdown(
                    f"**🎯 Decisão:** sinal **{sig_icon} {sig_lab}** · spread **{live_spread_pips:.2f}p** · "
                    f"confirmação ao vivo: **{status}** · sessão `{session}`: "
                    f"{'✅ dentro' if sess_ok else '⏳ fora'} do horário."
                )

                # -------- Poços de liquidez: distância ao vivo + sweep recente --------
                try:
                    from indicators import swing_distance, find_sweeps
                    _dist = swing_distance(df, c, pip_size=_pip_size)
                    _liq = []
                    if _dist.get("bsl_pips") is not None:
                        _liq.append(f"BSL acima a **+{_dist['bsl_pips']:.1f}p**")
                    if _dist.get("ssl_pips") is not None:
                        _liq.append(f"SSL abaixo a **-{_dist['ssl_pips']:.1f}p**")
                    _liq_txt = " · ".join(_liq) if _liq else "sem liquidez ativa nos últimos 150 candles"
                    st.markdown(f"🕳️ **Liquidez (SMC):** {_liq_txt}")

                    from indicators import get_current_wave
                    _w = get_current_wave(df)
                    if _w:
                        st.markdown(f"🌊 **Onda Atual ({_w['direction']}):** Início {_fmt_price(_w['origin'])} ➔ Extremo {_fmt_price(_w['extreme'])} (Onda: {_w['total_pct']:.2f}% | Hoje: {_w['day_pct']:+.2f}%)")
                        st.markdown(f"📐 **Retração (Correção):** 33% (1/3) em **{_fmt_price(_w['level_33'])}** | 50% em **{_fmt_price(_w['level_50'])}** | 66% (2/3) em **{_fmt_price(_w['level_66'])}**")

                    _sweeps = find_sweeps(df)
                    if _sweeps:
                        for _ev in _sweeps:
                            _lab = "🕳️ **BSL varrido**" if _ev["type"] == "BSL" else "🕳️ **SSL varrida**"
                            _side = "compras acima" if _ev["type"] == "BSL" else "vendas abaixo"
                            st.markdown(
                                f"{_lab} @ {_ev['price']:.5f} no candle "
                                f"{_ev['time'].strftime('%H:%M')} — pavio atravessou e fechou de volta. "
                                f"Possível spring (fakeout) buscando {_side}."
                            )
                except Exception as e:
                    st.caption(f"⚠️ Análise de liquidez indisponível: {e}")

                # -------- Score de confluência (0-8) para entrada manual --------
                try:
                    from indicators import confirmation_score
                    if sig == 0:
                        st.markdown("📋 **Sem sinal ativo no momento** — aguardando gatilho para calcular o score.")
                    else:
                        _score, _checks = confirmation_score(
                            df, sig, live_q=live_q,
                            max_spread_pips=float(max_spread),
                            pip_size=_pip_size,
                        )
                        _lab = "COMPRA" if sig == 1 else "VENDA"
                        _icon = "🟢" if sig == 1 else "🔴"
                        if _score >= 8:
                            _verdict = "✅ **confluência forte** — entrada com alta probabilidade"
                        elif _score >= 5:
                            _verdict = "⚠️ **confluência parcial** — espere mais uma confirmação"
                        else:
                            _verdict = "⛔ **sem confluência** — não é boa entrada"
                        st.markdown(f"📋 **Score de confirmação: {_score}/{len(_checks)}** ({_icon} {_lab}) — {_verdict}")
                        for _chk in _checks:
                            _mark = "✅" if _chk["ok"] else "❌"
                            _det = f" ({_chk['detail']})" if _chk["detail"] else ""
                            st.markdown(f"&nbsp;&nbsp;&nbsp;{_mark} {_chk['name']}{_det}")
                except Exception as e:
                    st.caption(f"⚠️ Score de confirmação indisponível: {e}")

                # -------- Clarezas: Status do fluxo institucional --------
                try:
                    _last = df.iloc[-1]
                    _clarezas_items = []
                    if "Clarezas_BuyFlow" in df.columns and _last.get("Clarezas_BuyFlow"):
                        _clarezas_items.append("🟢 **Fluxo de compra forte**")
                    if "Clarezas_SellFlow" in df.columns and _last.get("Clarezas_SellFlow"):
                        _clarezas_items.append("🔴 **Fluxo de venda forte**")
                    if "Clarezas_Absorption" in df.columns and _last.get("Clarezas_Absorption"):
                        _clarezas_items.append("🔵 **Absorção detectada** (indecisão institucional)")
                    if "Clarezas_ContinuationBuy" in df.columns and _last.get("Clarezas_ContinuationBuy"):
                        _clarezas_items.append("🟢 **Continuação de alta** (fluxo+pullback+rompimento)")
                    if "Clarezas_ContinuationSell" in df.columns and _last.get("Clarezas_ContinuationSell"):
                        _clarezas_items.append("🔴 **Continuação de baixa** (fluxo+pullback+rompimento)")
                    if _clarezas_items:
                        st.markdown("📊 **Clarezas:** " + " · ".join(_clarezas_items))
                except Exception:
                    pass

                # ── Motivo do sinal + R:R ──────────────────────────────────
                try:
                    _reason = df.attrs.get("signal_reason", "")
                    _rr = df.attrs.get("signal_rr")
                    _entry = df.attrs.get("signal_entry")
                    _sl = df.attrs.get("signal_sl")
                    _tp = df.attrs.get("signal_tp")

                    if sig != 0 and _reason:
                        st.markdown(
                            f"<div class='signal-new'>"
                            f"🔍 <b>Motivo do sinal:</b> {_reason}</div>",
                            unsafe_allow_html=True,
                        )

                    if sig != 0 and _rr is not None:
                        _rr_icon = "✅" if _rr >= 2 else ("⚠️" if _rr >= 1.5 else "⛔")
                        st.markdown(
                            f"<div class='signal-new'>"
                            f"📐 <b>Risco/Retorno:</b> {_rr_icon} <b>{_rr:.1f}R</b> "
                            f"(entrada {_fmt_price(_entry)} · SL {_fmt_price(_sl)} · TP {_fmt_price(_tp)})"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                except Exception:
                    pass

                # ── Painel de decisão rápida ────────────────────────────────
                if sig != 0:
                    _entry = df.attrs.get("signal_entry")
                    _sl = df.attrs.get("signal_sl")
                    _tp = df.attrs.get("signal_tp")
                    _rr = df.attrs.get("signal_rr")
                    _icon = "🟢" if sig == 1 else "🔴"
                    _lab = "COMPRA" if sig == 1 else "VENDA"

                    # Veredito baseado em R:R + score
                    _score_val = 0
                    try:
                        if sig != 0:
                            _score_val, _ = confirmation_score(df, sig, live_q=live_q,
                                                                max_spread_pips=float(max_spread),
                                                                pip_size=_pip_size)
                    except Exception:
                        pass

                    if _rr is not None and _rr >= 2 and _score_val >= 6:
                        _verdict = "✅ **ENTRAR** — confluência forte + R:R ≥ 2.0"
                        _panel_cls = "decision-enter"
                    elif _rr is not None and _rr >= 2 and _score_val >= 4:
                        _verdict = "⚠️ **AVALIAR** — R:R ok (≥2.0), mas confirmação parcial"
                        _panel_cls = "decision-evaluate"
                    else:
                        if _rr is not None and _rr < 2:
                            _verdict = f"⛔ **NÃO ENTRAR** — R:R ruim ({_rr:.1f}). Mínimo exigido: 2.0"
                        else:
                            _verdict = "⛔ **NÃO ENTRAR** — Falta de confluência"
                        _panel_cls = "decision-block"

                    st.markdown(
                        f"<div class='decision-panel {_panel_cls}'>"
                        f"<div class='d-title'>{_icon} DECISÃO: {_lab}</div>"
                        f"<div class='d-sub'>{_verdict}</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
            except Exception as e:
                st.caption(f"⚠️ Candle em formação indisponível: {e}")
    elif source == "awesomeapi":
        try:
            from dashboard_ui import quote_card
            api_key = get_secret("awesomeapi_key")
            live = get_provider(source="awesomeapi", api_key=api_key).get_last_quote(symbol)
            bid = float(live.get("bid", 0))
            ask = float(live.get("ask", bid))
            pct = float(live.get("pctChange", 0))
            _dir = 1 if pct > 0 else (-1 if pct < 0 else 0)
            cards = "".join([
                quote_card("BID (compra)", _fmt_price(bid)),
                quote_card("ASK (venda)", _fmt_price(ask), f"{pct:+.2f}% hoje", _dir),
                quote_card("MÁX. do dia", _fmt_price(float(live.get("high", 0)))),
                quote_card("MÍN. do dia", _fmt_price(float(live.get("low", 0)))),
            ])
            st.markdown(f"<div class='quote-strip'>{cards}</div>", unsafe_allow_html=True)
            st.caption(f"🔴 Fonte: **AwesomeAPI** em tempo real ({live.get('create_date', '')} UTC-3).")
        except Exception as e:
            st.caption(f"⚠️ Não foi possível obter a cotação ao vivo: {e}")

    vol_ok = volume_is_meaningful(df)
    if not vol_ok:
        st.info(
            "ℹ️ A fonte atual não entrega volume real de forex — Volume≈0. A confirmação "
            "dos sinais passou a usar **ATR em alta** em vez de volume. "
            "No AwesomeAPI o volume é o nº de ticks/candle; no Yahoo é zerado."
        )

    # ------------------------------------------------------------------ Métricas
    # Puxa o margin da IA para a gestão de risco dinâmico, se habilitado
    dyn_margin = 0.0
    if use_dynamic_risk:
        res_ml = train_cached(df)
        if res_ml[0] is not None:
            _, dyn_margin, _ = predict_latest(res_ml[0], df)

    col1, col2, col3, col4 = st.columns(4)

    last_close = df["Close"].iloc[-1]
    last_return = df["Return"].iloc[-1] if pd.notna(df["Return"].iloc[-1]) else 0.0
    last_rsi = df["RSI"].iloc[-1]
    last_signal = df["Signal"].iloc[signal_index(df, trigger_mode)]

    with col1:
        st.metric("Último preço", _fmt_price(last_close), f"{last_return * 100:.2f}%")

    with col2:
        rsi_status = "Sobrecomprado" if last_rsi > 70 else "Sobrevendido" if last_rsi < 30 else "Neutro"
        st.metric("RSI (14)", f"{last_rsi:.1f}", rsi_status)

    with col3:
        signal_label = {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[int(last_signal)]
        st.metric("Sinal por regras", signal_label)

    with col4:
        st.metric("Candles analisados", len(df))

    # Métrica do Estado do Mercado
    if "Market_Zone" in df.columns:
        current_zone = df["Market_Zone"].iloc[-1]
        zone_emoji = "⚖️" if current_zone == "EQUILIBRIO" else "🚀" if current_zone == "DESEQUILIBRIO" else "⚡"
        zone_label = "EQUILÍBRIO (lateral)" if current_zone == "EQUILIBRIO" else "DESEquilíbrio (tendência)" if current_zone == "DESEquilIBRIO" else "TRANSIÇÃO"
        st.metric(f"{zone_emoji} Estado do Mercado", current_zone, zone_label)

    # ------------------------------------------------- Volatilidade precisa
    if "Vol_Percentile" in df.columns:
        try:
            from volatility import vol_summary as _vol_summary
            _vs = _vol_summary(df)
            vc1, vc2, vc3 = st.columns(3)
            _emojis = {"EXTREMA": "🌋", "ALTA": "📈", "NORMAL": "⚖️", "BAIXA": "😴"}
            with vc1:
                _vpct = _vs["vol_pct"]
                st.metric(
                    "Volatilidade precisa",
                    f"{_vpct:.3f}%/candle" if _vpct is not None else "—",
                    f"{_emojis.get(_vs['regime'], '⚖️')} regime {_vs['regime'] or '—'}",
                )
            with vc2:
                _pct = _vs["percentile"]
                _pct_delta = "⛔ bloqueado" if (_pct is not None and _pct > vol_max_pct) else (
                    f"stop ×{_vs['stop_mult']:.2f}" if _vs["stop_mult"] > 1.0 else "stop ×1.00")
                st.metric("Percentil de vol (histórico)",
                          f"{_pct:.0f}" if _pct is not None else "—", _pct_delta)
            with vc3:
                _er = _vs["efficiency"]
                _er_delta = "⛔ lateral (chop)" if (_er is not None and _er < 0.15 and vol_er_gate) else (
                    "direção" if _er is not None else "—")
                st.metric("Eficiência (ER)", f"{_er:.2f}" if _er is not None else "—", _er_delta)
        except Exception:
            pass

    # ======================================================================
    # ABAS — separa os blocos para leitura rápida
    # ======================================================================
    tab_ov, tab_bt, tab_ia, tab_wfo, tab_j, tab_ex = st.tabs(
        ["📊 Visão geral", "🔍 Backtest", "🤖 Análise IA", "🧬 Otimizador WFO", "🗓️ Consistência", "💸 Execução"]
    )

    # ==============================================================
    # ABA: VISÃO GERAL — gráfico + RSI
    # ==============================================================
    with tab_ov:
        st.subheader("Gráfico de preço + médias + sinais")

        # Evita gráfico saturado: mostra só os últimos N candles por padrão,
        # com controle deslizante para ampliar/reduzir a janela visível.
        _max_plot = len(df)
        _default_plot = min(250, _max_plot)
        n_plot = st.slider("Candles visíveis (últimos N)", 20, _max_plot, _default_plot,
                           step=10, help="Mostra apenas os últimos N candles — deixa o gráfico legível. "
                                         "Mude o slider para ver mais/fazer zoom.")
        df_plot = df.iloc[-n_plot:]

        # Candle em formação atualizado com o tick ao vivo do MT5 (se o bridge
        # estiver rodando) — faz o gráfico "respirar" a cada refresh sem gastar
        # a cota da API. Indicadores/sinais continuam os do último candle fechado.
        if live_q and live_q.get("bid"):
            from model import apply_live_candle
            df_plot = apply_live_candle(df_plot, live_q)

        fig = go.Figure()
        
        if chart_type == "Barras (OHLC)":
            fig.add_trace(go.Ohlc(
                x=df_plot.index, open=df_plot["Open"], high=df_plot["High"], low=df_plot["Low"],
                close=df_plot["Close"], name="OHLC",
                increasing_line_color="#26A69A", decreasing_line_color="#EF5350",
                line=dict(width=1),
            ))
        else:
            fig.add_trace(go.Candlestick(
                x=df_plot.index, open=df_plot["Open"], high=df_plot["High"], low=df_plot["Low"],
                close=df_plot["Close"], name="OHLC",
                increasing_line_color="#26A69A", decreasing_line_color="#EF5350",
                increasing_fillcolor="#26A69A", decreasing_fillcolor="#EF5350",
                line=dict(width=1), whiskerwidth=0.4,
            ))
        if trigger_mode in ["auto", "cross", "slope", "3_9_20"]:
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_9"], name="MM 9",
                                     line=dict(color="#42A5F5", width=1)))
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_21"], name="MM 21",
                                     line=dict(color="#FFA726", width=1)))
        
        if trigger_mode != "fimathe_tokio":
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_200"], name="MM 200",
                                     line=dict(color="#AB47BC", width=1.5)))

        if show_bb and "BB_Upper" in df_plot.columns:
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["BB_Upper"], name="BB Sup (Resistência)",
                                     line=dict(color="rgba(171, 71, 188, 0.5)", width=1.5, dash="dot")))
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["BB_Lower"], name="BB Inf (Suporte)",
                                     line=dict(color="rgba(171, 71, 188, 0.5)", width=1.5, dash="dot")))

        if show_smart_cloud:
            # Copiamos para evitar SettingWithCopyWarning
            _df_cloud = df_plot.copy()
            _df_cloud["EMA_20"] = _df_cloud["Close"].ewm(span=20, adjust=False).mean()
            _df_cloud["EMA_50"] = _df_cloud["Close"].ewm(span=50, adjust=False).mean()
            
            # Série 1: Base EMA50 invisível para a nuvem de alta
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=_df_cloud["EMA_50"], 
                                     line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo='skip'))
            
            # Série 2: Nuvem de Alta (Verde Claro) - preenche apenas quando EMA20 > EMA50
            ema20_up = np.where(_df_cloud["EMA_20"] > _df_cloud["EMA_50"], _df_cloud["EMA_20"], _df_cloud["EMA_50"])
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=ema20_up, name="Cloud Alta",
                                     fill='tonexty', fillcolor='rgba(38, 166, 154, 0.3)', # Verde claro
                                     line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo='skip'))

            # Série 3: Base EMA50 invisível para a nuvem de baixa
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=_df_cloud["EMA_50"], 
                                     line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo='skip'))
            
            # Série 4: Nuvem de Baixa (Vermelho) - preenche apenas quando EMA20 < EMA50
            ema20_down = np.where(_df_cloud["EMA_20"] < _df_cloud["EMA_50"], _df_cloud["EMA_20"], _df_cloud["EMA_50"])
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=ema20_down, name="Cloud Baixa",
                                     fill='tonexty', fillcolor='rgba(239, 83, 80, 0.3)', # Vermelho
                                     line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo='skip'))
            
            # Traça as linhas EMA 20 e EMA 50 visíveis por cima
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=_df_cloud["EMA_20"], name="EMA 20 (Varejo)",
                                     line=dict(color="#26A69A", width=1.5)))
            fig.add_trace(go.Scatter(x=_df_cloud.index, y=_df_cloud["EMA_50"], name="EMA 50 (Institucional)",
                                     line=dict(color="#EF5350", width=2)))
            
            # Sinais da Nuvem (Cruzamento EMA20 e EMA50)
            cross_up = (_df_cloud["EMA_20"] > _df_cloud["EMA_50"]) & (_df_cloud["EMA_20"].shift(1) <= _df_cloud["EMA_50"].shift(1))
            cross_down = (_df_cloud["EMA_20"] < _df_cloud["EMA_50"]) & (_df_cloud["EMA_20"].shift(1) >= _df_cloud["EMA_50"].shift(1))
            
            fig.add_trace(go.Scatter(x=_df_cloud[cross_up].index, y=_df_cloud[cross_up]["Low"] * 0.9995, mode="markers",
                                      marker=dict(symbol="triangle-up", size=10, color="#26A69A", line=dict(width=1, color="white")), name="Cloud Buy"))
            fig.add_trace(go.Scatter(x=_df_cloud[cross_down].index, y=_df_cloud[cross_down]["High"] * 1.0005, mode="markers",
                                      marker=dict(symbol="triangle-down", size=10, color="#EF5350", line=dict(width=1, color="white")), name="Cloud Sell"))

        # ── Estado do Mercado (fundo colorido) ──────────────────────────────
        if show_market_state and "Market_Zone" in df_plot.columns:
            # Marcadores de TRANSIÇÃO (estrela amarela)
            if "Transition" in df_plot.columns:
                trans = df_plot[df_plot["Transition"]]
                if not trans.empty:
                    fig.add_trace(go.Scatter(x=trans.index, y=trans["High"] * 1.002, mode="markers",
                                              marker=dict(symbol="star", size=16, color="#FFD700", 
                                                          line=dict(width=2, color="white")),
                                              name="⚡ TRANSIÇÃO"))
            
            # Fundo: faixas verticais para cada estado
            zones = df_plot["Market_Zone"].values
            colors = []
            for z in zones:
                if z == "EQUILIBRIO":
                    colors.append("rgba(38, 166, 154, 0.06)")  # Verde claro
                elif z == "DESEQUILIBRIO":
                    colors.append("rgba(239, 83, 80, 0.06)")   # Vermelho claro
                else:
                    colors.append("rgba(255, 215, 0, 0.06)")   # Amarelo
            
            fig.add_trace(go.Bar(x=df_plot.index, y=[1]*len(df_plot), 
                                  marker=dict(color=colors, line_width=0),
                                  name="Estado", showlegend=False, hoverinfo="skip",
                                  yaxis="y2", opacity=0.5))

        buys = df_plot[df_plot["Signal"] == 1]
        sells = df_plot[df_plot["Signal"] == -1]
        
        # Setas grandes e brilhantes para fácil visualização
        fig.add_trace(go.Scatter(x=buys.index, y=buys["Low"] * 0.9990, mode="markers",
                                  marker=dict(symbol="triangle-up", size=22, color="#00FF00", line=dict(width=2, color="black")), name="Sinal COMPRA"))
        fig.add_trace(go.Scatter(x=sells.index, y=sells["High"] * 1.0010, mode="markers",
                                  marker=dict(symbol="triangle-down", size=22, color="#FF0000", line=dict(width=2, color="black")), name="Sinal VENDA"))

        # ── Clarezas: marcadores no gráfico ──────────────────────────────
        if "Clarezas_BuyFlow" in df_plot.columns:
            _bf = df_plot[df_plot["Clarezas_BuyFlow"]]
            _sf = df_plot[df_plot["Clarezas_SellFlow"]]
            _abs = df_plot[df_plot["Clarezas_Absorption"]] if "Clarezas_Absorption" in df_plot.columns else df_plot.iloc[0:0]
            _cb = df_plot[df_plot["Clarezas_ContinuationBuy"]] if "Clarezas_ContinuationBuy" in df_plot.columns else df_plot.iloc[0:0]
            _cs = df_plot[df_plot["Clarezas_ContinuationSell"]] if "Clarezas_ContinuationSell" in df_plot.columns else df_plot.iloc[0:0]

            # Fluxo: círculo pequeno no topo/fundo do candle
            fig.add_trace(go.Scatter(x=_bf.index, y=_bf["Low"] * 0.9985, mode="markers",
                                      marker=dict(symbol="circle", size=8, color="#26A69A", line=dict(width=1, color="white")),
                                      name="Fluxo Compra", opacity=0.8))
            fig.add_trace(go.Scatter(x=_sf.index, y=_sf["High"] * 1.0015, mode="markers",
                                      marker=dict(symbol="circle", size=8, color="#EF5350", line=dict(width=1, color="white")),
                                      name="Fluxo Venda", opacity=0.8))

            # Absorção: diamante (indecisão)
            if not _abs.empty:
                fig.add_trace(go.Scatter(x=_abs.index, y=_abs["Low"] * 0.9980, mode="markers",
                                          marker=dict(symbol="diamond", size=10, color="#42A5F5", line=dict(width=1, color="white")),
                                          name="Absorção", opacity=0.9))

            # Continuação: estrela (sinal mais forte)
            if not _cb.empty:
                fig.add_trace(go.Scatter(x=_cb.index, y=_cb["Low"] * 0.9975, mode="markers",
                                          marker=dict(symbol="star", size=14, color="#00FF00", line=dict(width=1, color="white")),
                                          name="Cont. Compra"))
            if not _cs.empty:
                fig.add_trace(go.Scatter(x=_cs.index, y=_cs["High"] * 1.0025, mode="markers",
                                          marker=dict(symbol="star", size=14, color="#FF0000", line=dict(width=1, color="white")),
                                          name="Cont. Venda"))

        # Marcador da posição aberta no par atual (manual ou automática)
        if exec_enabled:
            try:
                _open_pos = None
                if broker_type == "Paper (demo)":
                    from executor import PaperBroker
                    _pb = PaperBroker(slippage_pips=float(slippage_pips),
                                      initial_capital=float(exec_capital))
                    _open_pos = _pb.get_positions().get(symbol)
                else:
                    from executor import Mt5Broker, mt5_symbol
                    _open_pos = Mt5Broker().get_positions().get(mt5_symbol(symbol))
                if _open_pos and _open_pos.get("entry"):
                    _e = _open_pos["entry"]
                    _mark = _open_pos["side"]
                    _plabel = "LONG" if _mark == 1 else "SHORT"
                    fig.add_hline(y=_e, line_dash="dot", line_color="#FFD54F",
                                  annotation_text=f"🔓 Posição {_plabel} @ {_e:.5f}",
                                  annotation_position="top right")
                    if _open_pos.get("sl"):
                        fig.add_hline(y=_open_pos["sl"], line_dash="dash", line_color="#EF5350",
                                      annotation_text=f"SL {_open_pos['sl']:.5f}")
                    if _open_pos.get("tp"):
                        fig.add_hline(y=_open_pos["tp"], line_dash="dash", line_color="#26A69A",
                                      annotation_text=f"TP {_open_pos['tp']:.5f}")
            except Exception:
                pass
                
        # Define a margem direita (espaço vazio de 15 candles) para o gráfico respirar
        offset_map = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
        minutes = offset_map.get(interval, 15)
        future_date = df_plot.index[-1] + pd.Timedelta(minutes=minutes * 15)
        
        # Zonas de Liquidez (SMC FVG e BSL/SSL) no gráfico
        if smc_alerts_enabled:
            from indicators import get_active_fvgs, get_active_swings, find_sweeps
            bull_gaps, bear_gaps = get_active_fvgs(df_plot)
            bsl, ssl = get_active_swings(df_plot)
            
            shapes = []
            
            # FVGs (Zonas de Desequilíbrio institucionais)
            for g in bull_gaps:
                shapes.append(dict(
                    type="rect", xref="x", yref="y",
                    x0=g["time"], x1=future_date,
                    y0=g["bot"], y1=g["top"],
                    fillcolor="rgba(38, 166, 154, 0.15)", line_width=0, layer="below"
                ))
            for g in bear_gaps:
                shapes.append(dict(
                    type="rect", xref="x", yref="y",
                    x0=g["time"], x1=future_date,
                    y0=g["bot"], y1=g["top"],
                    fillcolor="rgba(239, 83, 80, 0.15)", line_width=0, layer="below"
                ))
                
            # Liquidez Externa (Buy-Side e Sell-Side) - "Poços da Sardinhada"
            for b in bsl:
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=b["time"], x1=future_date,
                    y0=b["price"], y1=b["price"],
                    line=dict(color="orange", width=1.5, dash="dashdot"), layer="below"
                ))
            for s in ssl:
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=s["time"], x1=future_date,
                    y0=s["price"], y1=s["price"],
                    line=dict(color="purple", width=1.5, dash="dashdot"), layer="below"
                ))
                
            # Retrações de Fibonacci (Onda atual 33%, 50%, 66%)
            from indicators import get_current_wave
            _w_plot = get_current_wave(df_plot)
            if _w_plot:
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=_w_plot["start_time"], x1=future_date,
                    y0=_w_plot["level_33"], y1=_w_plot["level_33"],
                    line=dict(color="lightblue", width=1.2, dash="dot"), layer="below"
                ))
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=_w_plot["start_time"], x1=future_date,
                    y0=_w_plot["level_50"], y1=_w_plot["level_50"],
                    line=dict(color="silver", width=1.2, dash="dot"), layer="below"
                ))
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=_w_plot["start_time"], x1=future_date,
                    y0=_w_plot["level_66"], y1=_w_plot["level_66"],
                    line=dict(color="gold", width=1.2, dash="dot"), layer="below"
                ))

            # Sweep recente (wick atravessou BSL/SSL e fechou de volta) — spring/fakeout
            _sweeps = find_sweeps(df)
            _sweep_in = [e for e in _sweeps if df_plot.index[0] <= e["time"] <= df_plot.index[-1]]
            if _sweep_in:
                fig.add_trace(go.Scatter(
                    x=[e["time"] for e in _sweep_in],
                    y=[e["price"] for e in _sweep_in],
                    mode="markers",
                    marker=dict(
                        symbol="diamond-open", size=12,
                        color=["#FF9800" if e["type"] == "BSL" else "#AB47BC" for e in _sweep_in],
                        line=dict(width=2),
                    ),
                    name="Sweep (wick+reversão)",
                    hovertemplate="%{x}<br>Sweep %{text} @ %{y:.5f}<extra></extra>",
                    text=[e["type"] for e in _sweep_in],
                ))
                
            if shapes:
                fig.update_layout(shapes=shapes)

        # Perfil de Volume (estilo Profit Pro) — barras horizontais na lateral direita
        _vp = None
        if vol_profile_on:
            try:
                from indicators import build_volume_profile_plotly
                _vp = build_volume_profile_plotly(df_plot, price_ref=float(df_plot["Close"].iloc[-1]))
                if _vp:
                    _vp_y, _vp_x, _vp_colors = _vp
                    fig.add_trace(go.Bar(
                        x=_vp_x, y=_vp_y,
                        orientation="h",
                        xaxis="x2",
                        marker=dict(color=_vp_colors),
                        opacity=0.55,
                        name="Vol. por preço",
                        hovertemplate="Preço %{y:.5f}<br>Vol %{x:.0f}<extra></extra>",
                    ))
            except Exception:
                _vp = None

        # Histograma de Volume Vertical (Tick Volume da MT5)
        vol_colors = ['#26A69A' if row['Close'] >= row['Open'] else '#EF5350' for idx, row in df_plot.iterrows()]
        fig.add_trace(go.Bar(
            x=df_plot.index, y=df_plot["Volume"],
            marker_color=vol_colors,
            name="Volume (Ticks)",
            yaxis="y2",
            opacity=0.4,
            hovertemplate="Vol: %{y:.0f}<extra></extra>"
        ))

        fig.update_layout(
            height=650,
            template="plotly_white",
            hovermode="x unified",
            title=f"{symbol.replace('=X', '')} — {interval} (últimos {len(df_plot)} candles)",
            xaxis_rangeslider_visible=False,
            dragmode="pan",
            xaxis=dict(fixedrange=False),
            yaxis=dict(fixedrange=False, domain=[0.25, 1.0]),  # Preço ocupa os 75% superiores
            yaxis2=dict(domain=[0.0, 0.20], showticklabels=False, showgrid=False),  # Volume ocupa os 20% inferiores
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        if _vp:
            fig.update_layout(
                xaxis=dict(fixedrange=False, domain=[0.0, 0.92]),
                xaxis2=dict(
                    anchor="y", side="right",
                    domain=[0.92, 1.0],
                    showticklabels=False, showgrid=False, zeroline=False,
                    range=[0, max(_vp_x) * 1.05],
                ),
            )
        # Traço invisível no futuro para forçar o Plotly a mostrar a margem direita
        fig.add_trace(go.Scatter(
            x=[future_date], y=[df_plot["Close"].iloc[-1]], 
            mode="markers", marker=dict(color="rgba(0,0,0,0)"), 
            showlegend=False, hoverinfo="skip"
        ))

        # Linha horizontal do preço atual + etiqueta no eixo Y direito
        _cur_price = float(df_plot["Close"].iloc[-1])
        fig.add_hline(
            y=_cur_price, line_color="#2563EB", line_width=1, line_dash="dot",
            layer="above",
        )
        fig.add_annotation(
            x=future_date, y=_cur_price,
            text=f"<b>{_fmt_price(_cur_price)}</b>",
            showarrow=False,
            font=dict(size=11, color="white"),
            bgcolor="#2563EB",
            bordercolor="#2563EB",
            borderwidth=1,
            xanchor="left", yanchor="middle",
        )

        fig.update_layout(xaxis=dict(
            range=[df_plot.index[0], future_date],
            rangeslider=dict(thickness=0.08),
        ))
        st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG)

        # RSI detalhado
        with st.expander("RSI (14) detalhado"):
            fig_rsi = go.Figure()
            fig_rsi.add_trace(go.Scatter(x=df.index, y=df["RSI"], name="RSI", line=dict(color="#2563EB")))
            fig_rsi.add_hline(y=rsi_ob, line_dash="dash", line_color="#dc2626", annotation_text=f"limite compra {rsi_ob}")
            fig_rsi.add_hline(y=rsi_os, line_dash="dash", line_color="#059669", annotation_text=f"limite venda {rsi_os}")
            fig_rsi.update_layout(height=250, template="plotly_white")
            st.plotly_chart(fig_rsi, use_container_width=True, config=_PLOTLY_CONFIG)

        # Volatilidade precisa: percentil do regime + σ por candle
        if "Vol_Percentile" in df.columns:
            with st.expander("🌋 Volatilidade precisa (percentil do regime + stop adaptativo)"):
                fig_vol = go.Figure()
                fig_vol.add_trace(go.Scatter(
                    x=df.index, y=df["Vol_Percentile"], name="Percentil de vol",
                    line=dict(color="#F59E0B", width=2),
                ))
                fig_vol.add_hline(y=float(vol_max_pct), line_dash="dash", line_color="#dc2626",
                                  annotation_text=f"bloqueio P{int(vol_max_pct)}")
                fig_vol.add_hline(y=75, line_dash="dot", line_color="#9ca3af", annotation_text="ALTA")
                fig_vol.add_hline(y=50, line_dash="dot", line_color="#9ca3af", annotation_text="mediana")
                fig_vol.add_trace(go.Scatter(
                    x=df.index, y=df["Vol_Stop_Mult"] * 50, name="Stop × (escala/50)",
                    line=dict(color="#2563EB", width=1, dash="dot"), yaxis="y2",
                ))
                fig_vol.update_layout(
                    height=280, template="plotly_white",
                    yaxis=dict(title="Percentil (0-100)", range=[-5, 105]),
                    yaxis2=dict(title="Stop ×", overlaying="y", side="right",
                                range=[40, 90], showgrid=False),
                    legend=dict(orientation="h", y=1.12),
                )
                st.plotly_chart(fig_vol, use_container_width=True, config=_PLOTLY_CONFIG)
                _vs_now = df.iloc[-1]
                st.caption(
                    f"Último candle: σ **{float(_vs_now.get('Vol_Precise_Pct') or 0):.3f}%**/candle · "
                    f"percentil **{float(_vs_now.get('Vol_Percentile') or 0):.0f}** "
                    f"(**{_vs_now.get('Vol_Regime') or '—'}**) · "
                    f"stop **×{float(_vs_now.get('Vol_Stop_Mult') or 1.0):.2f}** · "
                    f"ER **{float(_vs_now.get('Efficiency_Ratio') or 0):.2f}** "
                    "(0=lateral, 1=tendência). Estimadores: Parkinson + Garman-Klass + Yang-Zhang + realized."
                )

    # ==============================================================
    # ABA: BACKTEST
    # ==============================================================
    with tab_bt:
        st.caption(
            "Simulação com custo real (spread), SL/TP por ATR e risco % por trade. "
            "Resultado negativo = a estratégia perde para o custo da operação."
        )
        try:
            trades_df, metrics, equity = run_backtest(
                df,
                spread_pips=float(spread_pips),
                sl_atr=float(sl_atr),
                tp_atr=float(tp_atr),
                tp_pips=float(tp_pips),
                trailing_stop_pips=float(trailing_stop_pips),
                dynamic_risk_margin=float(dyn_margin),
                swap_pips_per_day=float(swap_pips_per_day),
                risk_per_trade_pct=float(risk_pct),
                pip_size=_pip_size,
                vol_stop=bool(vol_adaptive_stop),
            )
            verdict = summarize(metrics)
            st.warning(verdict)

            cols = st.columns(4)
            cols[0].metric("Retorno total", f"{metrics['Retorno total %']}%" if metrics["Retorno total %"] is not None else "—")
            cols[1].metric("Nº de trades", metrics["Nº de trades"])
            cols[2].metric("Taxa de acerto", f"{metrics['Taxa de acerto %']}%" if metrics["Taxa de acerto %"] is not None else "—")
            cols[3].metric("Profit factor", metrics["Profit factor"] if metrics["Profit factor"] is not None else "—")

            cols = st.columns(4)
            cols[0].metric("Expectativa/trade", f"${metrics['Expectativa por trade']}" if metrics["Expectativa por trade"] is not None else "—")
            cols[1].metric("Drawdown máx", f"{metrics['Drawdown máximo %']}%" if metrics["Drawdown máximo %"] is not None else "—")
            cols[2].metric("Sharpe (diário)", metrics["Sharpe (diário)"] if metrics["Sharpe (diário)"] is not None else "—")
            cols[3].metric("Capital final", f"${metrics['Capital final']}" if metrics["Capital final"] is not None else "—")

            if len(equity) > 1:
                fig_eq = go.Figure()
                fig_eq.add_trace(go.Scatter(x=equity.index, y=equity.values, name="Equity", line=dict(color="#059669")))
                fig_eq.update_layout(height=300, template="plotly_white", title="Curva de equity (marcada a mercado)")
                st.plotly_chart(fig_eq, use_container_width=True, config=_PLOTLY_CONFIG)

            if not trades_df.empty:
                with st.expander(f"Tabela de operações ({len(trades_df)})"):
                    st.dataframe(trades_df)
            else:
                st.info("Nenhum sinal no período com os parâmetros atuais.")
        except (ValueError, KeyError) as e:
            st.warning(str(e))

    # ==============================================================
    # ABA: ANÁLISE IA — ML + parecer
    # ==============================================================
    with tab_ia:
        st.subheader("Modelo de Machine Learning")
        st.caption(
            "Validação **walk-forward**: o modelo é re-treinado a cada passo e testado "
            "sempre em dados que nunca viu. O número honesto é o **edge** (acurácia − baseline)."
        )
        result = train_cached(df)
        if result[0] is None and result[1].get("error"):
            st.warning(result[1]["error"])
        else:
            model, metrics, (X_test, y_test) = result
            pred, margin, p = predict_latest(model, df)
            edge = metrics.get("wf_edge", metrics["accuracy"] - metrics["baseline"])
            acc = metrics["accuracy"] * 100
            base = metrics["baseline"] * 100

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                st.metric("Acurácia (walk-forward)", f"{acc:.1f}%")
                st.caption(f"Baseline: {base:.1f}%")
            with col_b:
                st.metric("Edge (acurácia − baseline)", f"{edge * 100:+.1f}%",
                          "vantagem real" if edge > 0.02 else "sem vantagem")
            with col_c:
                pred_label = "🟢 Subir" if pred == 1 else "🔴 Cair"
                st.metric("Previsão p/ próximo candle", pred_label)
                st.caption(f"Confiança = margem {margin * 100:.1f}%")

            if edge <= 0.02:
                st.warning(
                    f"⚠️ O edge ({edge * 100:+.1f}%) é ~0 ou negativo: o modelo acerta igual a "
                    "chutar o lado mais comum. Resultado honesto para previsão de direção de curtíssimo prazo."
                )
            elif margin < 0.05:
                st.warning("⚠️ A margem de confiança é < 5% — cara ou coroa neste candle.")

            with st.expander("Relatório de classificação (última janela)"):
                st.dataframe(pd.DataFrame(metrics["report"]).transpose())

        # Análise de risco IA (opcional)
        if ai_enabled:
            import ai_analysis as ai_mod
            st.subheader("Análise de probabilidade e risco (IA)")
            rule_signal = int(df["Signal"].iloc[signal_index(df, trigger_mode)])
            ml_pred = None
            ml_p = None
            if result[0] is not None:
                _pred2, _margin2, _p2 = predict_latest(result[0], df)
                ml_pred, ml_p = int(_pred2), float(_p2)
            ml_dir = None if ml_pred is None else (1 if ml_pred == 1 else -1)

            direction = rule_signal if rule_signal != 0 else ml_dir
            if direction is None:
                st.info("Sinal das regras neutro e ML sem previsão — sem direção para analisar.")
            else:
                _src = "regras" if rule_signal != 0 else "ML (regras neutras)"

                try:
                    res = ai_analysis_cached(
                        df, direction, float(sl_atr), float(tp_atr), float(spread_pips),
                        _pip_size, ml_p, ml_dir, ai_horizon, ai_paths, llm_config=llm_config
                    )
                except (ValueError, KeyError) as e:
                    st.warning(str(e))
                    res = None

                if res:
                    s, mc = res["stats"], res["mc"]
                    dir_label = {1: "🟢 COMPRA (LONG)", -1: "🔴 VENDA (SHORT)"}[direction]
                    st.caption(f"Direção analisada: **{dir_label}** (origem: {_src}) · "
                               f"preço {_fmt_price(s['price'])} · {s['vol_label']} · {s['trend_label']}")

                    score = res["score"]
                    score_status = "cara ou coroa" if score < 55 else "leve vantagem" if score < 65 else "vantagem relevante"
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Score de convicção (0-100)", f"{score:.0f}", f"{score_status}")
                    c2.metric("P(TP) Monte Carlo", f"{mc['p_tp'] * 100:.1f}%",
                              f"P(SL) {mc['p_sl'] * 100:.1f}%")
                    c3.metric("Expectativa (R)", f"{mc['expected_r']:+.2f}R",
                              f"mediana {mc['median_r']:+.2f}R")
                    c4.metric("P(trade lucrativo)", f"{mc['p_r_positive'] * 100:.1f}%",
                              f"VaR95 {s['var95_pct']:.2f}%")

                    # histograma do Monte Carlo
                    if mc.get("r_flat_samples") is not None:
                        fig_mc = go.Figure()
                        fig_mc.add_trace(go.Histogram(x=mc["r_flat_samples"], nbinsx=60,
                                                      name="Distribuição de R", marker_color="#2563EB", opacity=0.85))
                        fig_mc.add_vline(x=0, line_color="#d97706", line_dash="dot", annotation_text="breakeven (0R)")
                        fig_mc.add_vline(x=1, line_color="#059669", line_dash="dash", annotation_text="TP (+1R)")
                        fig_mc.add_vline(x=-1, line_color="#dc2626", line_dash="dash", annotation_text="SL (-1R)")
                        fig_mc.update_layout(
                            height=280, template="plotly_white",
                            title=f"Monte Carlo — distribuição do retorno (R) em {mc['n_paths']} caminhos",
                            xaxis_title="R (1R = perda do SL)", yaxis_title="caminhos", bargap=0.05,
                        )
                        st.plotly_chart(fig_mc, use_container_width=True, config=_PLOTLY_CONFIG)

                    parecer = res.get("parecer")
                    if parecer:
                        st.markdown(f"### 🤖 Parecer do Analista IA (LLM)\n> {parecer}")
                    elif llm_config:
                        st.error("Falha ao gerar o parecer LLM. Verifique sua chave da API e se as dependências (google-generativeai/openai) estão instaladas.")

                    if score < 55:
                        st.warning(
                            f"⚠️ Score de convicção {score:.0f} — estatisticamente indistinguível de moeda "
                            "lançada. A análise recomenda NÃO operar esta configuração agora."
                        )

                    with st.expander("Detalhes técnicos da análise"):
                        _detail = {
                            "Drift por candle": f"{s['mu_candle'] * 1e5:.1f} (10⁻⁵) · σ {s['sigma_candle'] * 1e4:.1f} (10⁻⁴)",
                            "Volatilidade": f"{s['vol_candle_pct']:.2f}%/candle",
                            "ATR": _fmt_price(s["atr"]),
                            "Freq. histórica da direção": f"{s['p_hist_dir'] * 100:.1f}%",
                            "Distância TP / SL": f"{_fmt_price(s['tp_dist'])} / {_fmt_price(s['sl_dist'])}",
                            "P(TP) fórmula (drift)": f"{s['p_tp_formula'] * 100:.1f}%",
                            "VaR 95% / 99% (1 candle)": f"{s['var95_pct']:.2f}% / {s['var99_pct']:.2f}%",
                            "RSI": f"{s['rsi']:.1f}",
                            "MACD histograma": "positivo" if s["macd_hist"] > 0 else "negativo",
                            "BB position": f"{s['bb_pos']:+.2f}",
                            "MM9 vs MM21": "alta acima" if s["trend_align"] else "alta abaixo",
                        }
                        st.dataframe(pd.DataFrame(list(_detail.items()), columns=["Métrica", "Valor"]),
                                     hide_index=True)

    # ==============================================================
    # ABA: OTIMIZADOR WFO (Walk-Forward Optimization)
    # ==============================================================
    with tab_wfo:
        st.subheader("🧬 Otimização Walk-Forward Contínua (WFO)")
        st.caption(
            "Em vez de fixar parâmetros para sempre, sistemas institucionais otimizam os tamanhos das médias "
            "e níveis do RSI constantemente em janelas de treinamento, avaliando em janelas de teste (fora-da-amostra). "
            "Isso adapta a estratégia às mudanças de regime de mercado sem incorrer em *curve fitting* (overfitting)."
        )
        
        wfo_top = st.number_input("Quantos resultados mostrar?", min_value=1, max_value=20, value=5)
        
        if st.button("🚀 Rodar Otimização Agora (Pode levar de 1 a 3 minutos)", type="primary"):
            with st.spinner("Testando centenas de combinações em treino e avaliando no teste..."):
                import optimize
                # Utiliza o dataframe bruto já baixado
                ranked = optimize.run_optimization_workflow(df, float(spread_pips), _pip_size, trigger_mode=trigger_mode)
                if not ranked:
                    st.warning("Não houveram dados/trades suficientes para avaliar com robustez.")
                else:
                    st.success("Otimização concluída! Resultados que sobreviveram à janela Fora-da-Amostra (Teste):")
                    
                    show_data = []
                    for params, tm, em in ranked[:int(wfo_top)]:
                        cross = "X" if params["use_cross"] else "Nível"
                        sess = "Sim" if params["session"] else "Não"
                        show_data.append({
                            "RSI OB/OS": f"{params['rsi_overbought']}/{params['rsi_oversold']}",
                            "Filtro ADX": params['min_adx'],
                            "Stop Loss (xATR)": params['sl_atr'],
                            "Take Profit (xATR)": params['tp_atr'],
                            "WinRate Treino": f"{tm['hit']}%",
                            "Trades Treino": tm['n'],
                            "Expectativa Teste (R)": f"{em['exp']:+.3f}" if em else "—",
                            "Trades Teste": em['n'] if em else 0,
                        })
                    st.dataframe(pd.DataFrame(show_data), hide_index=True)
                    
                    st.info(
                        "**Como ler:** Escolha sempre as configurações que tiveram um bom número de trades no Treino "
                        "mas que mantiveram a 'Expectativa Teste' positiva. Atualize o painel lateral com esses parâmetros."
                    )

    # ==============================================================
    # ABA: CONSISTÊNCIA — diário da qualidade de execução
    # ==============================================================
    with tab_j:
        import journal as journal_mod

        st.subheader("🗓️ Diário de consistência")
        st.caption(
            "O foco não é o dinheiro, o gráfico ou o indicador — é **agir como o mercado**: "
            "ler a zona de liquidez, esperar a zona neutra, identificar a armadilha e só entrar "
            "na confirmação do movimento, como um produto na feira. "
            "Aqui você mede se está **executando o plano** (não se ganhou dinheiro)."
        )

        with st.expander("➕ Registrar operação", expanded=True):
            jc1, jc2, jc3 = st.columns(3)
            with jc1:
                j_sym = st.selectbox("Par", _awesome_symbols + _yfinance_symbols,
                                     index=0 if source == "awesomeapi" else 5)
            with jc2:
                j_side = st.selectbox("Direção", ["Compra (LONG)", "Venda (SHORT)"])
            with jc3:
                j_zone = st.selectbox(
                    "Zona da entrada",
                    [journal_mod.ZONE_LIQUIDEZ, journal_mod.ZONE_NEUTRA, journal_mod.ZONE_ARMADILHA],
                    format_func=lambda z: journal_mod.ZONE_LABEL[z],
                )

            jp1, jp2, jp3 = st.columns(3)
            with jp1:
                j_entry = st.number_input("Preço entrada", value=0.0, step=0.0001)
            with jp2:
                j_exit = st.number_input("Preço saída", value=0.0, step=0.0001)
            with jp3:
                j_size = st.number_input("Tamanho (unidades/lotes)", value=0.0, step=0.01)

            jc4, jc5, jc6 = st.columns(3)
            with jc4:
                j_conf = st.text_input("Confirmação (ex.: rompeu resistência + vela de corpo)",
                                       value="")
            with jc5:
                j_follow = st.checkbox("Segui o plano (sem hesitar/emocionar)", value=True)
            with jc6:
                j_comment = st.text_input("Observação", value="")

            if st.button("💾 Salvar operação", type="primary"):
                if j_entry and j_exit and j_size:
                    journal_mod.add_entry(
                        symbol=j_sym, side=1 if "Compra" in j_side else -1,
                        entry_price=j_entry, exit_price=j_exit, size=j_size,
                        zone=j_zone, confirmation=j_conf,
                        followed_plan=j_follow, comment=j_comment,
                    )
                    st.success("Registrada. O que importa agora é: segui o plano?")
                else:
                    st.error("Preencha entrada, saída e tamanho.")

        j_state = journal_mod._load_state()
        j_s = journal_mod.summary(j_state)
        entries = j_state.get("entries", [])

        if entries:
            jm1, jm2, jm3, jm4 = st.columns(4)
            jm1.metric("Trades registrados", j_s["n"])
            jm2.metric("Consistência (segui o plano)",
                       f"{j_s['pct_followed']}%" if j_s["pct_followed"] is not None else "—",
                       "o que mais importa" if (j_s["pct_followed"] or 0) >= 80 else "melhore a disciplina")
            jm3.metric("Acerto geral", f"{j_s['wins'] / j_s['n'] * 100:.1f}%" if j_s["n"] else "—")
            jm4.metric("Expectativa (R/trade)", f"{j_s['exp_r']:+.4f}R" if j_s["exp_r"] is not None else "—",
                       "positiva = leitura somando" if (j_s["exp_r"] or 0) > 0 else "negativa = revisar leitura")

            st.subheader("🕳️ Leitura por zona")
            rows = []
            for z in journal_mod.ZONES:
                bz = j_s["by_zone"].get(z, {"n": 0, "wins": 0, "hit": 0, "confirms": 0, "lab": journal_mod.ZONE_LABEL[z]})
                rows.append({
                    "Zona": bz["lab"],
                    "Trades": bz["n"],
                    "Acerto %": f"{bz['hit']:.1f}%" if bz["n"] else "—",
                    "Com confirmação": bz["confirms"],
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True)
            st.caption(
                "Leitura honesta: zona com muitas operações e acerto baixo = você está forçando "
                "lá. Zona com poucas operações = sua leitura ainda não encontrou confirmação — ok."
            )

            if len(j_s["equity"]) > 1:
                eq = pd.DataFrame(j_s["equity"])
                try:
                    eq["ts"] = pd.to_datetime(eq["ts"])
                except Exception:
                    pass
                fig_j = go.Figure(go.Scatter(x=eq["ts"], y=eq["equity"], mode="lines+markers",
                                             name="Execução (R acumulado)"))
                fig_j.update_layout(title="Curva de execução (qualidade do plano, em R)",
                                    height=280, template="plotly_white")
                st.plotly_chart(fig_j, use_container_width=True, config=_PLOTLY_CONFIG)

            with st.expander(f"📋 Histórico ({len(entries)})"):
                df_j = pd.DataFrame(entries)
                if not df_j.empty:
                    df_j["Direção"] = df_j["side"].map({1: "LONG", -1: "SHORT"})
                    df_j["Plano"] = df_j["followed_plan"].map({True: "✅ segui", False: "❌ furou"})
                    show = df_j.rename(columns={
                        "symbol": "Par", "entry_price": "Entrada", "exit_price": "Saída",
                        "size": "Tamanho", "confirmation": "Confirmação", "comment": "Obs", "ts": "Data",
                    })
                    cols = ["Data", "Par", "Direção", "Entrada", "Saída", "Tamanho", "Plano", "Confirmação", "Obs"]
                    st.dataframe(show[[c for c in cols if c in show.columns]], hide_index=True)
                if st.button("🗑️ Apagar última operação"):
                    journal_mod.delete_entry(len(entries) - 1)
                    st.success("Última operação removida.")
        else:
            st.info(
                "Nenhuma operação registrada ainda. O diário funciona assim: você já lê o mercado "
                "há 6 anos — registre CADA operação manual com a zona e se seguiu o plano. "
                "Em 20-30 trades você verá onde sua leitura acerta e onde você está forçando."
            )

        st.markdown("---")
        st.subheader("🔗 Dashboard MT5 (Automático)")
        st.caption("Estatísticas e operações puxadas diretamente da corretora.")
        try:
            from mt5_bridge import get_bridge
            br = get_bridge()
            if br.available():
                deals = br.history_deals_get(days=30)
                if not deals:
                    df_deals = pd.DataFrame(columns=['time', 'symbol', 'type', 'volume', 'price', 'profit'])
                else:
                    df_deals = pd.DataFrame(deals)
                    df_deals['time'] = pd.to_datetime(df_deals['time'], unit='s')

                if not df_deals.empty:
                    df_deals['cumulative_profit'] = df_deals['profit'].cumsum()
                    total_trades = len(df_deals)
                    wins = len(df_deals[df_deals['profit'] > 0])
                    losses = len(df_deals[df_deals['profit'] < 0])
                    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
                    total_profit = df_deals['profit'].sum()
                else:
                    df_deals['cumulative_profit'] = []
                    total_trades = 0
                    wins = 0
                    losses = 0
                    win_rate = 0
                    total_profit = 0.0

                # Cards de Métricas Principais
                dc1, dc2, dc3, dc4 = st.columns(4)
                dc1.metric("Resultado Total", f"${total_profit:.2f}", f"{win_rate:.1f}% acerto")
                dc2.metric("Operações Vencedoras", wins)
                dc3.metric("Operações Perdedoras", losses)
                dc4.metric("Total de Trades", total_trades)

                # Gráfico da Curva de Capital
                st.markdown("**Curva de Capital (Acumulado)**")
                if not df_deals.empty:
                    fig_eq_mt5 = go.Figure(go.Scatter(x=df_deals['time'], y=df_deals['cumulative_profit'], mode="lines", name="Capital", line=dict(color="#38bdf8", width=2)))
                else:
                    fig_eq_mt5 = go.Figure()
                    fig_eq_mt5.add_annotation(text="Sem operações no período", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
                fig_eq_mt5.update_layout(height=300, template="plotly_white", margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig_eq_mt5, use_container_width=True, config=_PLOTLY_CONFIG)

                # Tabela de Últimos Trades
                with st.expander("Últimas Posições Fechadas (MT5)", expanded=True):
                    if not df_deals.empty:
                        df_deals['Tipo'] = df_deals['type'].map({0: "BUY", 1: "SELL"}).fillna("OTHER")
                        df_show = df_deals[['time', 'symbol', 'Tipo', 'volume', 'price', 'profit']].tail(10).iloc[::-1]
                        df_show = df_show.rename(columns={'time': 'Data/Hora', 'symbol': 'Ativo', 'volume': 'Lote', 'price': 'Preço', 'profit': 'Lucro ($)'})
                        st.dataframe(df_show, hide_index=True)
                    else:
                        st.info("Nenhum histórico de posições fechadas.")
            else:
                st.warning("⚠️ MT5 Bridge indisponível. Confirme se o mt5server.exe está rodando.")
        except Exception as e:
            st.error(f"Erro ao carregar dados do MT5: {e}")

    # ==============================================================
    # ABA: EXECUÇÃO — alertas Telegram + paper/MT5
    # ==============================================================
    with tab_ex:
        if alerts_enabled:
            st.subheader("📣 Alertas Telegram")
            if not alerts_mod.configured():
                st.warning("📣 Alertas marcados, mas faltam telegram_token/telegram_chat no secrets.toml.")
            else:
                # O candle acionável não é sempre o último: o pullback_liquidity
                # sinaliza no candle FECHADO (-2). Fonte única: signal_index().
                _sig_idx = signal_index(df, trigger_mode)
                msg = alerts_mod.notify_signal_change(symbol, int(df["Signal"].iloc[_sig_idx]),
                                                      float(df["Close"].iloc[_sig_idx]), interval)
                if msg:
                    st.success(f"📣 Alerta enviado ao Telegram:\n\n{msg}")
                else:
                    st.caption("📣 Alertas ativos — sinal só notifica quando muda.")

        if exec_enabled:
            if broker_type == "Paper (demo)":
                st.subheader("💸 Execução ao vivo — modo demo (paper trading)")
                broker = PaperBroker(slippage_pips=float(slippage_pips), initial_capital=float(exec_capital))
                if st.session_state.get("_reset_exec"):
                    broker.reset(initial_capital=float(exec_capital))
                    st.session_state["_reset_exec"] = False
                    st.success("Conta demo zerada.")
            else:
                st.subheader("💸 Execução real/demo — MetaTrader 5")
                from executor import Mt5Broker
                broker = Mt5Broker()
                if st.session_state.get("_reset_exec"):
                    broker.reset()
                    st.session_state["_reset_exec"] = False
                    st.success("Posições do MT5 encerradas.")
                if not broker.available():
                    st.error(
                        "MT5 não disponível. Verifique se o terminal está aberto/logado "
                        "e o bridge rodando (veja README → `start_mt5.sh`)."
                    )

            # Candle ACIONÁVEL: o pullback_liquidity grava em -2, não em -1.
            _ai = signal_index(df, trigger_mode)
            last_signal = int(df["Signal"].iloc[_ai])
            last_close = float(df["Close"].iloc[_ai])
            last_atr = float(df["ATR"].iloc[_ai]) if pd.notna(df["ATR"].iloc[_ai]) else 0.0

            # Stops explícitos da estratégia (ex.: Fimathe — SL/TP por canal).
            # O broker os usa exatamente como o backtest para validação honesta.
            _sug_sl = 0.0
            _sug_tp = 0.0
            if "Suggested_SL" in df.columns and pd.notna(df["Suggested_SL"].iloc[_ai]):
                _sug_sl = float(df["Suggested_SL"].iloc[_ai])
            if "Suggested_TP" in df.columns and pd.notna(df["Suggested_TP"].iloc[_ai]):
                _sug_tp = float(df["Suggested_TP"].iloc[_ai])

            # Multiplicador do regime de volatilidade (stop adaptativo)
            _vol_mult = 1.0
            if vol_adaptive_stop and "Vol_Stop_Mult" in df.columns:
                _vm = df["Vol_Stop_Mult"].iloc[_ai]
                if pd.notna(_vm) and float(_vm) > 0:
                    _vol_mult = float(_vm)

            # Mesmo lock do cron: o fragmento roda a cada 10 s enquanto o cron
            # roda a cada 5 min — sem mutex os dois liam "sem posição" juntos.
            from safety import execution_lock, trading_allowed
            with execution_lock(blocking=False) as _got_lock:
                if not _got_lock:
                    msg = "⏳ Outro executor ativo (cron) — tick do dashboard ignorado."
                else:
                    _allowed, _why = trading_allowed()
                    if not _allowed:
                        msg = f"🛑 Trading desativado ({_why})"
                    else:
                        msg = broker.on_signal(
                            symbol, last_signal, last_close, last_atr,
                            sl_atr=float(sl_atr), tp_atr=float(tp_atr), risk_pct=float(risk_pct), tp_pips=float(tp_pips),
                            trailing_stop_pips=float(trailing_stop_pips), dynamic_risk_margin=float(dyn_margin),
                            max_open_positions=int(max_open_positions),
                            suggested_sl=_sug_sl, suggested_tp=_sug_tp, pip_size=_pip_size,
                            vol_mult=_vol_mult,
                        )
            if msg:
                st.info(msg)

            summ = broker.summary()
            c1, c2, c3, c4, c5 = st.columns(5)
            if broker_type == "Paper (demo)":
                _rp = summ.get("return_pct")
                c1.metric("Capital (demo)", f"${summ['capital']:,.2f}",
                          (f"{_rp:+.2f}%" if _rp is not None else None))
                c2.metric("PnL total", f"${summ['pnl_total']:,.2f}")
                c3.metric("Trades", summ["trades"])
                c4.metric("Vencedores", summ["wins"])
                c5.metric("Sinal atual", {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal])
            else:
                if summ.get("error"):
                    st.error(f"MT5: {summ['error']}")
                c1.metric("Equity (MT5)", f"${summ.get('capital', 0):,.2f}")
                c2.metric("Saldo (MT5)", f"${summ.get('initial_capital', 0):,.2f}")
                c3.metric("Lucro realizado (30d robô)", f"${summ.get('pnl_total', 0):,.2f}")
                c4.metric("Posições do robô", summ.get("open_positions", 0))
                c5.metric("Sinal atual", {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal])
                _mode = summ.get("account_mode") or "unknown"
                if _mode == "real":
                    from executor import live_trading_enabled
                    st.warning(
                        f"⚠️ **CONTA REAL** — login `{summ.get('login')}` · "
                        f"{summ.get('company') or summ.get('server')}. "
                        + ("`ALLOW_LIVE_TRADING=1` ativo: ordens vão para dinheiro de verdade."
                           if live_trading_enabled() else
                           "Entradas BLOQUEADAS até `ALLOW_LIVE_TRADING=1`.")
                    )
                elif _mode == "demo":
                    st.caption(f"Conta **demo** · login `{summ.get('login')}` · "
                               f"servidor `{summ.get('server')}`")
                else:
                    st.warning(f"Modo da conta desconhecido ({_mode}) — entradas bloqueadas.")
                if summ.get("margin_free") is not None:
                    st.caption(f"Margem livre: {float(summ['margin_free']):,.2f}")

            from safety import kill_switch_reason
            _ks = kill_switch_reason()
            if _ks:
                st.error(f"🛑 **KILL-SWITCH ATIVO** — {_ks}  "
                         f"(arquivo `TRADING_DISABLED`; reative com "
                         f"`python safety.py enable`)")

            positions = broker.get_positions()
            if positions:
                # chave do MT5 é o símbolo SEM hífen (XAUUSD), a do app tem (XAU-USD)
                from executor import mt5_symbol as _m5sym
                pos = positions.get(symbol) or positions.get(_m5sym(symbol))
                if pos:
                    side = "LONG" if pos["side"] == 1 else "SHORT"
                    st.write(f"**Posição aberta ({symbol}):** {side} @ {pos['entry']:.5f} "
                             f"| SL {pos['sl']:.5f} | TP {pos['tp']:.5f}")

            # Execução manual
            with st.expander("🎮 Execução manual", expanded=True):
                st.caption("Abre/fecha posição à mão, independente do sinal automático.")
                from executor import CONTRACT_SIZE
                mc1, mc2, mc3 = st.columns(3)
                with mc1:
                    manual_side = st.selectbox("Lado", ["Compra (LONG)", "Venda (SHORT)"])
                with mc2:
                    manual_lots = st.number_input("Volume (lotes)", min_value=0.01, max_value=50.0,
                                                  value=0.01, step=0.01)
                with mc3:
                    auto_sl_tp = st.checkbox("SL/TP automático (ATR)", value=True)
                manual_sl = 0.0
                manual_tp = 0.0
                if not auto_sl_tp:
                    ms1, ms2 = st.columns(2)
                    manual_sl = ms1.number_input("SL (preço)", value=0.0, step=0.0001, help="0 = sem stop")
                    manual_tp = ms2.number_input("TP (preço)", value=0.0, step=0.0001, help="0 = sem take")

                b1, b2, b3 = st.columns(3)
                if b1.button("🚀 Comprar agora", type="primary"):
                    _side = 1
                    _sl = manual_sl or (last_close - sl_atr * last_atr * _vol_mult)
                    _tp = manual_tp or (last_close + tp_atr * last_atr * _vol_mult)
                    _size = manual_lots * CONTRACT_SIZE if broker_type == "Paper (demo)" else manual_lots
                    _ok = broker.place_order(symbol, _side, last_close, _sl, _tp, _size)
                    _msg2 = getattr(broker, "_last_msg", "")
                    st.success(f"{'✅ Ordem enviada' if _ok else '❌ Falha na ordem'} — COMPRA {symbol} {manual_lots} lotes"
                               f"{f' (MT5: {_msg2})' if _msg2 else ''}")
                if b2.button("🚀 Vender agora", type="primary"):
                    _side = -1
                    _sl = manual_sl or (last_close + sl_atr * last_atr * _vol_mult)
                    _tp = manual_tp or (last_close - tp_atr * last_atr * _vol_mult)
                    _size = manual_lots * CONTRACT_SIZE if broker_type == "Paper (demo)" else manual_lots
                    _ok = broker.place_order(symbol, _side, last_close, _sl, _tp, _size)
                    _msg2 = getattr(broker, "_last_msg", "")
                    st.success(f"{'✅ Ordem enviada' if _ok else '❌ Falha na ordem'} — VENDA {symbol} {manual_lots} lotes"
                               f"{f' (MT5: {_msg2})' if _msg2 else ''}")
                if b3.button("🔒 Fechar posição"):
                    _pnl = broker.close_position(symbol, last_close)
                    if _pnl is None:
                        st.info("Nenhuma posição aberta para fechar.")
                    else:
                        st.success(f"Posição fechada — PnL {_pnl:+.2f}")

            if broker_type == "MetaTrader 5":
                with st.expander("📋 Ordens Pendentes", expanded=False):
                    st.caption("Pendure ordens BUY_LIMIT, SELL_LIMIT, BUY_STOP ou SELL_STOP no MT5.")
                    from mt5_bridge import get_bridge as _get_bridge2
                    from executor import mt5_symbol as _mt5_sym
                    _br2 = _get_bridge2()
                    if _br2.available():
                        pc1, pc2 = st.columns(2)
                        with pc1:
                            _pend_type = st.selectbox("Tipo", ["BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"])
                        with pc2:
                            _pend_lots = st.number_input("Volume (lotes)", min_value=0.01, max_value=50.0, value=0.01, step=0.01, key="pend_lots")

                        pp1, pp2, pp3 = st.columns(3)
                        with pp1:
                            _pend_price = st.number_input("Preço gatilho", value=0.0, step=0.01, format="%.5f", key="pend_price")
                        with pp2:
                            _pend_sl = st.number_input("SL (0 = sem)", value=0.0, step=0.01, format="%.5f", key="pend_sl")
                        with pp3:
                            _pend_tp = st.number_input("TP (0 = sem)", value=0.0, step=0.01, format="%.5f", key="pend_tp")

                        if st.button("📌 Pendurar ordem", type="primary", key="btn_pend"):
                            if _pend_price <= 0:
                                st.error("Informe um preço de gatilho válido.")
                            else:
                                _res = _br2.place_pending(
                                    symbol=_mt5_sym(symbol),
                                    order_type=_pend_type,
                                    volume=_pend_lots,
                                    price=_pend_price,
                                    sl=_pend_sl if _pend_sl > 0 else None,
                                    tp=_pend_tp if _pend_tp > 0 else None,
                                )
                                _code = getattr(_res, "retcode", None)
                                _comment = str(getattr(_res, "comment", "")) or str(_code)
                                if _code == 10009:
                                    st.success(f"✅ Ordem pendente {_pend_type} {symbol} @ {_pend_price:.5f} — {_comment}")
                                else:
                                    st.error(f"❌ Falha: {_comment}")

                        _pend = _br2.pending_orders()
                        if _pend:
                            st.markdown("**Ordens pendentes ativas:**")
                            _df_pend = pd.DataFrame(_pend)
                            _df_pend['time_setup'] = pd.to_datetime(_df_pend['time_setup'], unit='s')
                            _df_pend = _df_pend.rename(columns={
                                'ticket': 'Ticket', 'symbol': 'Ativo', 'type_name': 'Tipo',
                                'volume': 'Lote', 'price': 'Preço', 'sl': 'SL', 'tp': 'TP', 'time_setup': 'Criada em'
                            })
                            st.dataframe(_df_pend[['Ticket', 'Ativo', 'Tipo', 'Lote', 'Preço', 'SL', 'TP', 'Criada em']], hide_index=True, use_container_width=True)

                            _cancel_ticket = st.number_input("Ticket para cancelar", value=0, step=1, key="cancel_ticket")
                            if st.button("❌ Cancelar ordem", key="btn_cancel"):
                                if _cancel_ticket > 0:
                                    _res2 = _br2.cancel_order(_cancel_ticket)
                                    _code2 = getattr(_res2, "retcode", None)
                                    if _code2 == 10009:
                                        st.success(f"✅ Ordem {_cancel_ticket} cancelada.")
                                    else:
                                        st.error(f"❌ Falha ao cancelar: {getattr(_res2, 'comment', _code2)}")
                                else:
                                    st.warning("Informe o ticket da ordem.")
                        else:
                            st.info("Nenhuma ordem pendente ativa.")
                    else:
                        st.warning("MT5 indisponível para ordens pendentes.")

            with st.expander(f"Histórico de trades", expanded=True):
                if broker_type == "Paper (demo)":
                    hist = broker.state.get("history", [])
                    if hist:
                        _df_hist = pd.DataFrame(hist)
                        _df_hist['PnL'] = _df_hist['pnl'].astype(float)
                        _df_hist['Resultado'] = _df_hist['PnL'].apply(lambda x: '✅ Gain' if x > 0 else '❌ Loss')
                        _df_hist['Dia'] = pd.to_datetime(_df_hist['exit_ts']).dt.date

                        _total = len(_df_hist)
                        _gains = len(_df_hist[_df_hist['PnL'] > 0])
                        _losses = len(_df_hist[_df_hist['PnL'] <= 0])
                        _win_rate = (_gains / _total * 100) if _total > 0 else 0
                        _total_pnl = _df_hist['PnL'].sum()

                        mc1, mc2, mc3, mc4 = st.columns(4)
                        mc1.metric("Total de trades", _total)
                        mc2.metric("🟢 Gains", f"{_gains}", f"{_win_rate:.1f}% acerto")
                        mc3.metric("🔴 Losses", f"{_losses}", f"{100 - _win_rate:.1f}% erro")
                        mc4.metric("💰 PnL Total", f"${_total_pnl:+,.2f}")

                        _daily = _df_hist.groupby('Dia').agg(
                            Trades=('PnL', 'count'),
                            Gains=('PnL', lambda x: (x > 0).sum()),
                            Losses=('PnL', lambda x: (x <= 0).sum()),
                            Lucro=('PnL', 'sum')
                        ).reset_index()
                        _daily['Acerto %'] = (_daily['Gains'] / _daily['Trades'] * 100).round(1)
                        st.markdown("**Trades por dia:**")
                        st.dataframe(_daily, hide_index=True, use_container_width=True)

                        _df_hist['Data'] = pd.to_datetime(_df_hist['exit_ts']).dt.strftime('%d/%m %H:%M')
                        _df_show = _df_hist[['Data', 'symbol', 'side', 'entry', 'exit', 'PnL', 'Resultado']].copy()
                        _df_show.columns = ['Data', 'Ativo', 'Lado', 'Entrada', 'Saída', 'Lucro ($)', 'Resultado']
                        st.markdown(trade_table_html(_df_show), unsafe_allow_html=True)
                    else:
                        st.info("Nenhum trade ainda. Os sinais aparecem aqui quando executarem no papel.")
                else:
                    try:
                        from mt5_bridge import get_bridge
                        _br = get_bridge()
                        if _br.available():
                            _debug = _br.debug_deals(days=30)
                            _deals = _debug.get("deals", [])
                            if _deals:
                                _df_deals = pd.DataFrame(_deals)
                                _df_deals['time'] = pd.to_datetime(_df_deals['time'], unit='s')
                                _df_deals['Tipo'] = _df_deals['type'].map({0: "BUY", 1: "SELL"}).fillna("OTHER")
                                _df_deals['Resultado'] = _df_deals['profit'].apply(lambda x: '✅ Gain' if x > 0 else '❌ Loss')
                                _df_deals['Dia'] = _df_deals['time'].dt.date

                                _total = len(_df_deals)
                                _gains = len(_df_deals[_df_deals['profit'] > 0])
                                _losses = len(_df_deals[_df_deals['profit'] <= 0])
                                _win_rate = (_gains / _total * 100) if _total > 0 else 0
                                _total_pnl = _df_deals['profit'].sum()

                                mc1, mc2, mc3, mc4 = st.columns(4)
                                mc1.metric("Total de trades", _total)
                                mc2.metric("🟢 Gains", f"{_gains}", f"{_win_rate:.1f}% acerto")
                                mc3.metric("🔴 Losses", f"{_losses}", f"{100 - _win_rate:.1f}% erro")
                                mc4.metric("💰 PnL Total", f"${_total_pnl:+,.2f}")

                                _daily = _df_deals.groupby('Dia').agg(
                                    Trades=('profit', 'count'),
                                    Gains=('profit', lambda x: (x > 0).sum()),
                                    Losses=('profit', lambda x: (x <= 0).sum()),
                                    Lucro=('profit', 'sum')
                                ).reset_index()
                                _daily['Acerto %'] = (_daily['Gains'] / _daily['Trades'] * 100).round(1)
                                st.markdown("**Trades por dia:**")
                                st.dataframe(_daily, hide_index=True, use_container_width=True)

                                _df_show = _df_deals[['time', 'symbol', 'Tipo', 'volume', 'price', 'profit', 'Resultado']].tail(20).iloc[::-1]
                                _df_show = _df_show.rename(columns={'time': 'Data/Hora', 'symbol': 'Ativo', 'volume': 'Lote', 'price': 'Preço', 'profit': 'Lucro ($)'})
                                st.markdown(trade_table_html(_df_show), unsafe_allow_html=True)
                            else:
                                st.warning(f"Nenhum trade encontrado via MT5. Total de deals brutos: {_debug.get('total', '?')}")
                                st.json(_debug)
                        else:
                            st.info("MT5 indisponível para buscar histórico.")
                    except Exception as e:
                        st.error(f"Erro ao buscar histórico: {e}")
        else:
            st.caption("Habilite 'Acompanhar sinais (paper/MT5)' na sidebar para ver esta aba.")

    # ------------------------------------------------------------------ Dados brutos
    with st.expander("📄 Dados brutos (últimos candles)"):
        st.dataframe(df.tail(200), use_container_width=True)

    # ------------------------------------------------------------------ Rodapé honesto
    st.markdown("---")
    with st.expander("⚠️ Leia antes de operar com dinheiro real"):
        st.markdown(
            """
            - **Nenhum indicador ou modelo prevê mercado com certeza.** Acurácia ~50-55%
              em direção de 1 candle é estatisticamente indistinguível de moeda lançada.
            - **Sinal ≠ lucro.** O backtest é o único lugar que mostra se a estratégia
              paga o custo (spread). Se o profit factor < 1.0, ela perde dinheiro.
            - **Dados:** valide qualquer sinal em dados de qualidade (CSV do MetaTrader/Dukascopy) antes.
            - **Risco:** jamais opere capital que você não pode perder. Comece em conta demo.
            """
        )
    st.caption(
        "⚠️ Ferramenta de análise, não garantia de resultado. "
        "O backtest usa dados históricos e ignora slippage, requotes e downtime."
    )


if auto_refresh:
    render_main = st.fragment(run_every=_refresh_opts[refresh_label])(render_main)
render_main()
