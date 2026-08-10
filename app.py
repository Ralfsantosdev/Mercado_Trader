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

from data_provider import get_provider
from indicators import add_all_indicators
from model import (
    generate_rule_based_signals,
    apply_live_confirmation,
    volume_is_meaningful,
    train_ml_model,
    predict_latest,
)
from backtest import run_backtest, summarize
from executor import PaperBroker
from dashboard_ui import inject_css, header

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
    ["awesomeapi", "yfinance"],
    index=0,
    help="awesomeapi: cotação e histórico em tempo real com a chave do "
         "https://awesomeapi.com.br (sem cache). yfinance: Yahoo Finance (cache de 1h).",
)

_awesome_symbols = [
    "EUR-USD", "GBP-USD", "USD-JPY", "AUD-USD", "USD-CAD", "NZD-USD", "USD-CHF",
    "EUR-GBP", "EUR-JPY", "XAU-USD", "USD-BRL", "EUR-BRL", "BTC-BRL", "ETH-BRL", "DOGE-BRL",
]
_yfinance_symbols = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]

symbol = st.sidebar.selectbox(
    "Par de moedas",
    _awesome_symbols if source == "awesomeapi" else _yfinance_symbols,
    format_func=lambda x: x.replace("=X", ""),
)

interval = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "30m", "1h", "1d"],
    index=4,
    help="awesomeapi fornece ticks de ~1 min e o app remonta candles no timeframe "
         "pedido (dados reais, diferente do Yahoo que não entrega forex intradiário). "
         "Para 1d usa o endpoint de fechamento diário.",
)

# Limites de histórico por fonte/timeframe
if source == "awesomeapi":
    # intraday: montado de ticks de 1 min via paginação (~30 dias); diário: até 360 dias
    _max_days = {"1m": 1, "5m": 30, "15m": 30, "30m": 30, "1h": 30, "1d": 360}[interval]
else:
    _max_days = {"1m": 7, "5m": 60, "15m": 60, "30m": 45, "1h": 365, "1d": 365}[interval]

# Streamlit slider requires min_value < max_value
_max_days = max(2, _max_days)
days = st.sidebar.slider("Período (dias de histórico)", 1, _max_days, min(30, _max_days))

st.sidebar.subheader("🎯 Sinal por regras")
rsi_ob = st.sidebar.slider("RSI sobrecompra (limite de compra)", 60, 95, 70)
rsi_os = st.sidebar.slider("RSI sobrevenda (limite de venda)", 5, 40, 30)
use_cross = st.sidebar.checkbox("Gatilho por cruzamento MM9×MM21", value=True,
                                help="Desmarque para usar nível (MM9 acima/abaixo da MM21) em vez de cruzamento.")
trigger_mode = st.sidebar.selectbox(
    "Gatilho do sinal",
    ["auto", "slope", "cross", "pullback", "3_9_20", "fimathe_tokio"],
    index=4,
    help="auto = comportamento antigo. pullback = espera o preço voltar à EMA. "
         "3_9_20 = Cruz. de 3 com 9 e 20 inclinada. "
         "fimathe_tokio = Técnica Fimathe (Rompimento CR na Abertura)."
)
pullback_ema = st.sidebar.slider("EMA do pullback", 13, 100, 50, step=1,
                                 help="Só usado quando gatilho = pullback.")

st.sidebar.subheader("⛩️ Configurações Fimathe")
fimathe_hour = st.sidebar.number_input("Hora de abertura (0-23)", 0, 23, 20, 
                                       help="Hora em que o mercado 'abre' para traçar o canal (ex: 20 para Tóquio, 8 para Londres).")
fimathe_candles = st.sidebar.number_input("Duração (número de candles)", 1, 20, 4,
                                          help="Quantos candles definem o Canal de Referência (CR). Ex: 4 candles no 5m = 20 minutos.")
fimathe_max_pips = st.sidebar.number_input("Max Tamanho (pips)", 10, 2000, 30,
                                           help="Se o CR ultrapassar isso, ele será cortado ao meio (fatiado) para diminuir o SL.")

use_volume = st.sidebar.checkbox("Confirmar com volume", value=True,
                                 help="Se o volume não existir na fonte (Yahoo forex), a confirmação usa ATR em alta.")

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

st.sidebar.subheader("💰 Backtest")
_is_gold = symbol.startswith("XAU")
_pip_size = 0.01 if _is_gold else 0.0001
_default_spread = 25.0 if _is_gold else 2.0
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
ai_enabled = st.sidebar.checkbox(
    "Ativar análise de probabilidade e risco (IA)",
    value=False,
    help="Estatística local (Monte Carlo, VaR, P(TP/SL)) — somente offline, "
         "sem chamadas de API e sem custo de tokens.",
)
use_dynamic_risk = st.sidebar.checkbox(
    "Gestão de Risco Dinâmica (Kelly Fracionado via IA)",
    value=False,
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
    value=False,
    help="Usa o que o terminal MT5 oferece de verdade: bloqueia a entrada quando o "
         "spread real (ask-bid) estiver alto ou o volume de tick for baixo. "
         "Requer mt5server.exe rodando. Sem ele, o filtro fica desligado.",
)
max_spread = st.sidebar.number_input("Spread máximo ao vivo (pips)", min_value=0.5, max_value=100.0,
                                     value=5.0, step=0.5,
                                     help="Acima disso a entrada é bloqueada (economia paga o stop, "
                                          "ex.: em horário de notícia). 0 desliga o spread.")
min_tick_vol = st.sidebar.number_input("Volume de tick mínimo", min_value=0, max_value=1000,
                                       value=0, step=1,
                                       help="Entradas são bloqueadas quando o candle atual tem menos "
                                            "que isso de volume de tick real. 0 desliga.")

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

auto_refresh = st.sidebar.checkbox("Atualizar automaticamente (15s)", value=True)

if st.sidebar.button("🔄 Atualizar dados agora"):
    st.cache_data.clear()

st.sidebar.caption(
    "Fonte: **AwesomeAPI** em tempo real (chave em `.streamlit/secrets.toml`) "
    "ou **Yahoo Finance (yfinance)**. Troca em 1 linha em `data_provider.py`."
)


# ---------------------------------------------------------------------------
# Pipeline de dados (cacheado) e ML (cacheado) — definidos fora do fragment
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def load_and_prepare(source, symbol, days, interval, rsi_ob, rsi_os, use_cross, trigger_mode, pullback_ema,
                     session, min_adx, max_spread_pips, min_tick_volume, fimathe_hour, fimathe_candles, fimathe_max_pips):
    api_key = get_secret("awesomeapi_key") if source == "awesomeapi" else None
    provider = get_provider(source=source, api_key=api_key)
    try:
        df = provider.get_ohlcv(symbol, days=days, interval=interval)
    except Exception as e:
        # erro de API/chave → deixa o app mostrar a mensagem, sem dado para trabalhar
        import streamlit as st
        st.session_state["_data_error"] = str(e)
        return pd.DataFrame()
    st.session_state["_data_error"] = ""
    if df.empty:
        return df

    df["Return"] = df["Close"].pct_change()
    df["Direction"] = (df["Return"].shift(-1) > 0).astype(int)
    df = add_all_indicators(df)
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
                       ml_p, ml_pred, horizon, n_paths):
    import ai_analysis
    return ai_analysis.run_analysis(
        df, direction, sl_atr=sl_atr, tp_atr=tp_atr, spread_pips=spread_pips,
        pip_size=pip_size, ml_p=ml_p, ml_pred=ml_pred,
        horizon=horizon, n_paths=n_paths, llm_config=None,
    )


# ---------------------------------------------------------------------------
# Corpo do dashboard — envolto em fragment p/ auto-refresh (60s)
# ---------------------------------------------------------------------------
def _mt5_live_quote(symbol):
    """Busca quote real do MT5 (spread/tick volume). None se bridge indisponível."""
    try:
        from mt5_bridge import MT5Bridge
        m = MT5Bridge()
        if not m.available():
            return None
        m.initialize()
        q = m.quote(symbol.replace("-", ""))
        m.shutdown()
        return q if q.get("bid", 0) else None
    except Exception:
        return None


def render_main():
    with st.spinner("Buscando dados..."):
        df = load_and_prepare(source, symbol, days, interval, rsi_ob, rsi_os, use_cross, trigger_mode, pullback_ema,
                              session, min_adx, float(spread_pips * 2), 5,
                              int(fimathe_hour), int(fimathe_candles), float(fimathe_max_pips))

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
    mt5_block_msg = ""
    if mt5_filter:
        live_q = _mt5_live_quote(symbol)
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

    # ------------------------------------------------------------ Cotação ao vivo
    if source == "awesomeapi":
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
    last_signal = df["Signal"].iloc[-1]

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

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df_plot.index, open=df_plot["Open"], high=df_plot["High"], low=df_plot["Low"],
            close=df_plot["Close"], name="OHLC",
            increasing_line_color="#26A69A", decreasing_line_color="#EF5350",
            increasing_fillcolor="#26A69A", decreasing_fillcolor="#EF5350",
            line=dict(width=1), whiskerwidth=0.4,
        ))
        fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_9"], name="MM 9",
                                 line=dict(color="#42A5F5", width=1)))
        fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_21"], name="MM 21",
                                 line=dict(color="#FFA726", width=1)))
        fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["SMA_200"], name="MM 200",
                                 line=dict(color="#AB47BC", width=1.5)))

        buys = df_plot[df_plot["Signal"] == 1]
        sells = df_plot[df_plot["Signal"] == -1]
        fig.add_trace(go.Scatter(x=buys.index, y=buys["Low"] * 0.9995, mode="markers",
                                  marker=dict(symbol="triangle-up", size=9, color="lime"), name="Compra"))
        fig.add_trace(go.Scatter(x=sells.index, y=sells["High"] * 1.0005, mode="markers",
                                  marker=dict(symbol="triangle-down", size=9, color="red"), name="Venda"))

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

        fig.update_layout(
            height=600,
            template="plotly_white",
            hovermode="x unified",
            title=f"{symbol.replace('=X', '')} — {interval} (últimos {len(df_plot)} candles)",
            xaxis_rangeslider_visible=True,
            xaxis=dict(fixedrange=False),
            yaxis=dict(fixedrange=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_xaxes(rangeslider_thickness=0.08)
        st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG)

        # RSI detalhado
        with st.expander("RSI (14) detalhado"):
            fig_rsi = go.Figure()
            fig_rsi.add_trace(go.Scatter(x=df.index, y=df["RSI"], name="RSI", line=dict(color="#2563EB")))
            fig_rsi.add_hline(y=rsi_ob, line_dash="dash", line_color="#dc2626", annotation_text=f"limite compra {rsi_ob}")
            fig_rsi.add_hline(y=rsi_os, line_dash="dash", line_color="#059669", annotation_text=f"limite venda {rsi_os}")
            fig_rsi.update_layout(height=250, template="plotly_white")
            st.plotly_chart(fig_rsi, use_container_width=True, config=_PLOTLY_CONFIG)

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
            rule_signal = int(df["Signal"].iloc[-1])
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
                        _pip_size, ml_p, ml_dir, ai_horizon, ai_paths,
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

                    # parecer narrativo desligado definitivamente (zero custo de tokens)
                    st.info(
                        "ℹ️ O parecer narrativo da IA foi desligado por padrão para não gerar "
                        "custo de tokens. Estas métricas são 100% offline (Monte Carlo, VaR, P(TP/SL))."
                    )

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

    # ==============================================================
    # ABA: EXECUÇÃO — alertas Telegram + paper/MT5
    # ==============================================================
    with tab_ex:
        if alerts_enabled:
            st.subheader("📣 Alertas Telegram")
            if not alerts_mod.configured():
                st.warning("📣 Alertas marcados, mas faltam telegram_token/telegram_chat no secrets.toml.")
            else:
                msg = alerts_mod.notify_signal_change(symbol, int(df["Signal"].iloc[-1]),
                                                      float(df["Close"].iloc[-1]), interval)
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

            last_signal = int(df["Signal"].iloc[-1])
            last_close = float(df["Close"].iloc[-1])
            last_atr = float(df["ATR"].iloc[-1]) if pd.notna(df["ATR"].iloc[-1]) else 0.0

            # Stops explícitos da estratégia (ex.: Fimathe — SL/TP por canal).
            # O broker os usa exatamente como o backtest para validação honesta.
            _sug_sl = 0.0
            _sug_tp = 0.0
            if "Suggested_SL" in df.columns and pd.notna(df["Suggested_SL"].iloc[-1]):
                _sug_sl = float(df["Suggested_SL"].iloc[-1])
            if "Suggested_TP" in df.columns and pd.notna(df["Suggested_TP"].iloc[-1]):
                _sug_tp = float(df["Suggested_TP"].iloc[-1])

            msg = broker.on_signal(
                symbol, last_signal, last_close, last_atr,
                sl_atr=float(sl_atr), tp_atr=float(tp_atr), risk_pct=float(risk_pct), tp_pips=float(tp_pips),
                trailing_stop_pips=float(trailing_stop_pips), dynamic_risk_margin=float(dyn_margin),
                max_open_positions=int(max_open_positions),
                suggested_sl=_sug_sl, suggested_tp=_sug_tp, pip_size=_pip_size,
            )
            if msg:
                st.info(msg)

            summ = broker.summary()
            c1, c2, c3, c4, c5 = st.columns(5)
            if broker_type == "Paper (demo)":
                c1.metric("Capital (demo)", f"${summ['capital']:,.2f}", f"{summ['return_pct']:+.2f}%")
                c2.metric("PnL total", f"${summ['pnl_total']:,.2f}")
                c3.metric("Trades", summ["trades"])
                c4.metric("Vencedores", summ["wins"])
                c5.metric("Sinal atual", {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal])
            else:
                c1.metric("Equity (MT5)", f"${summ.get('capital', 0):,.2f}")
                c2.metric("Saldo (MT5)", f"${summ.get('initial_capital', 0):,.2f}")
                c3.metric("Lucro flutuante", f"${summ.get('pnl_total', 0):,.2f}")
                c4.metric("Posições abertas", summ.get("open_positions", 0))
                c5.metric("Sinal atual", {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal])
                if summ.get("login"):
                    st.caption(f"Conta: **{summ.get('company')}** · login `{summ.get('login')}` · "
                               f"servidor `{summ.get('server')}`")

            positions = broker.get_positions()
            if positions:
                pos = positions.get(symbol)
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
                    _sl = manual_sl or (last_close - sl_atr * last_atr)
                    _tp = manual_tp or (last_close + tp_atr * last_atr)
                    _size = manual_lots * CONTRACT_SIZE if broker_type == "Paper (demo)" else manual_lots
                    _ok = broker.place_order(symbol, _side, last_close, _sl, _tp, _size)
                    _msg2 = getattr(broker, "_last_msg", "")
                    st.success(f"{'✅ Ordem enviada' if _ok else '❌ Falha na ordem'} — COMPRA {symbol} {manual_lots} lotes"
                               f"{f' (MT5: {_msg2})' if _msg2 else ''}")
                if b2.button("🚀 Vender agora", type="primary"):
                    _side = -1
                    _sl = manual_sl or (last_close + sl_atr * last_atr)
                    _tp = manual_tp or (last_close - tp_atr * last_atr)
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

            with st.expander(f"Histórico de trades ({summ.get('trades', 0)})"):
                if broker_type == "Paper (demo)":
                    hist = broker.state.get("history", [])
                    if hist:
                        st.dataframe(pd.DataFrame(hist))
                    else:
                        st.info("Nenhum trade ainda. Os sinais aparecem aqui quando executarem no papel.")
                else:
                    st.info("Histórico de trades: veja a aba 'Histórico' no próprio MetaTrader 5.")
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
    render_main = st.fragment(run_every="15s")(render_main)
render_main()
