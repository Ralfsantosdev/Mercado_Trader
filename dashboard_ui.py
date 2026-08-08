"""
Dashboard_ui — visual premium do Forex Dashboard.

Estética "terminal de mesa de operações": fundo marinho-escuro com profundidade,
cartões glassmorphism, tipografia refinada (Manrope para leitura, JetBrains Mono
para números) e indicador de status ao vivo. Densidade controlada: forte, mas
sem ruído.
"""

import streamlit as st

# Paleta
ACCENT = "#38bdf8"      # ciano — destaque
ACCENT2 = "#818cf8"     # índigo secundário
UP = "#34d399"          # verde — compra/lucro
DOWN = "#fb7185"        # rosa/vermelho — venda/perda
GOLD = "#fbbf24"        # âmbar — atenção
BG = "#0b1120"          # fundo marinho-escuro
SURFACE = "#111a2e"     # cartões
SURFACE2 = "#0f172a"    # secundário
BORDER = "#1e293b"      # bordas
TEXT = "#e7ecf5"
MUTED = "#7c8aa5"

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {{
  --accent: {ACCENT};
  --up: {UP};
  --down: {DOWN};
  --gold: {GOLD};
  --bg: {BG};
  --surface: {SURFACE};
  --border: {BORDER};
  --text: {TEXT};
  --muted: {MUTED};
}}

/* ============================ Base ============================ */
html, body, [data-testid="stAppViewContainer"] {{
  background:
    radial-gradient(1100px 500px at 85% -10%, rgba(56,189,248,0.10), transparent 60%),
    radial-gradient(900px 600px at -10% 110%, rgba(99,102,241,0.08), transparent 55%),
    var(--bg);
  color: var(--text);
  font-family: 'Manrope', sans-serif;
}}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 2rem; }}

/* ============================ Cabeçalho premium ============================ */
.dash-header {{
  display: flex; align-items: flex-end; justify-content: space-between;
  padding: 6px 0 14px 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 18px;
}}
.dash-header .brand {{
  font-family: 'Manrope', sans-serif;
  font-weight: 800;
  font-size: 26px;
  letter-spacing: -0.5px;
  background: linear-gradient(120deg, #ffffff, var(--ACCENT));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.dash-header .sub {{
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 1.6px;
  text-transform: uppercase;
  margin-top: 3px;
}}
.live-pill {{
  display: inline-flex; align-items: center; gap: 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--muted); letter-spacing: 1px;
}}
.live-dot {{
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--up); box-shadow: 0 0 0 0 rgba(52,211,153,0.7);
  animation: pulse 2s infinite;
}}
@keyframes pulse {{
  0% {{ box-shadow: 0 0 0 0 rgba(52,211,153,0.6); }}
  70% {{ box-shadow: 0 0 0 8px rgba(52,211,153,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(52,211,153,0); }}
}}

/* ============================ Cartões de cotação ============================ */
.quote-strip {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }}
.q-card {{
  flex: 1; min-width: 150px;
  background: linear-gradient(180deg, #141f36, #0f172a);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px 18px 12px 18px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.04);
  position: relative; overflow: hidden;
}}
.q-card::before {{
  content:""; position: absolute; top:0; left:14px; right:14px; height:2px;
  background: linear-gradient(90deg, transparent, var(--ACCENT), transparent);
  opacity: .5;
}}
.q-card .lab {{ font-size: 10px; letter-spacing: 1.4px; text-transform: uppercase; color: var(--muted); }}
.q-card .val {{ font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700; margin-top: 4px; }}
.q-card .delta {{ font-size: 12px; font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }}
.q-up   .val, .q-up   .delta {{ color: var(--up); }}
.q-down .val, .q-down .delta {{ color: var(--down); }}

/* ============================ Badge de status (sinal) ============================ */
.signal-badge {{
  display:inline-flex; align-items:center; gap:8px;
  font-family:'Manrope'; font-weight:700; font-size:12px;
  padding:4px 12px; border-radius:999px; text-transform:uppercase; letter-spacing:.6px;
}}
.signal-buy {{ background: rgba(52,211,153,0.15); color: var(--up); border:1px solid rgba(52,211,153,0.4); }}
.signal-sell {{ background: rgba(251,113,133,0.15); color: var(--down); border:1px solid rgba(251,113,133,0.4); }}
.signal-none {{ background: rgba(139,138,160,0.12); color: var(--muted); border:1px solid rgba(139,138,160,0.3); }}

/* ============================ Métricas nativas ============================ */
div[data-testid="stMetric"] {{
  background: linear-gradient(180deg, #141f36, #0f172a);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px 16px 12px 16px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.04);
}}
div[data-testid="stMetricLabel"] {{ color: var(--muted); font-size: 11px; letter-spacing: .6px; text-transform: uppercase; }}
div[data-testid="stMetricValue"] {{ font-family: 'JetBrains Mono', monospace; font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }}
div[data-testid="stMetricDelta"] {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; }}

/* ============================ Texto / títulos ============================ */
h1, h2, h3, h4 {{ font-family: 'Manrope', sans-serif !important; font-weight: 700 !important; letter-spacing: -.2px; }}
h2 {{ border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-top: 28px; font-size: 1.25rem; }}
p, .stMarkdown {{ font-size: 14px; line-height: 1.6; }}

/* ============================ Tabs ============================ */
button[data-baseweb="tab"] {{
  font-family: 'Manrope'; font-weight:600; text-transform:uppercase; font-size:12px;
  letter-spacing:.6px;
}}
button[data-baseweb="tab-list"] {{ gap: 2px; }}

/* ============================ Expanders ============================ */
[data-testid="stExpander"] {{
  background: #0f172a; border: 1px solid var(--border); border-radius: 12px;
  box-shadow: 0 6px 18px rgba(0,0,0,0.25);
}}

/* ============================ Sidebar ============================ */
[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, #0d1526, #0b1120);
  border-right: 1px solid var(--border);
}}
[data-testid="stSidebar"] label, [data-testid="stWidgetLabel"] {{
  font-size: 11px !important; color: var(--muted) !important;
}}
[data-testid="stSidebar"] hr {{ border-color: var(--border); }}
[data-baseweb="select"] > div [data-testid="stInput"], [data-baseweb="input"] > div {{
  background: var(--surface) !important; border: 1px solid var(--border) !important;
}}

/* ============================ Botões ============================ */
.stButton > button {{
  background: linear-gradient(180deg, #16213a, #101a30);
  border: 1px solid var(--border); color: var(--text); border-radius: 10px;
  font-family: 'Manrope'; font-weight:600; transition: all .15s ease;
}}
.stButton > button:hover {{ border-color: var(--ACCENT); color: var(--ACCENT); box-shadow: 0 0 0 3px rgba(56,189,248,0.12); }}
.stButton > button[kind="primary"], .stButton > button[data-testid="stBaseButton-primary"] {{
  background: linear-gradient(90deg, var(--ACCENT), #2563eb); color:#04121f; border:none; font-weight:700;
}}

/* ============================ Tabelas ============================ */
[data-testid="stDataFrame"] {{ font-family: 'JetBrains Mono', monospace; font-size: 12.5px; }}

/* ============================ Alertas ============================ */
.stAlert {{ border-radius: 10px; font-size: 13px; }}
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def header(brand: str, sub: str, live_label: str = "AO VIVO") -> None:
    st.markdown(
        f"<div class='dash-header'>"
        f"<div><div class='brand'>{brand}</div><div class='sub'>{sub}</div></div>"
        f"<div class='live-pill'><span class='live-dot'></span>{live_label}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def signal_badge(signal: int) -> str:
    """Badge colorido do sinal (1 compra, -1 venda, 0 neutro)."""
    if signal == 1:
        return "<span class='signal-badge signal-buy'>▲ Compra</span>"
    if signal == -1:
        return "<span class='signal-badge signal-sell'>▼ Venda</span>"
    return "<span class='signal-badge signal-none'>● Neutro</span>"


def quote_card(label: str, value: str, delta: str = "", direction: int = 0) -> str:
    cls = "q-up" if direction == 1 else ("q-down" if direction == -1 else "")
    delta_html = f"<div class='delta'>{delta}</div>" if delta else ""
    return (
        f"<div class='q-card {cls}'><div class='lab'>{label}</div>"
        f"<div class='val'>{value}</div>{delta_html}</div>"
    )