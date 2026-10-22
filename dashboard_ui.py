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
  background: linear-gradient(90deg, var(--ACCENT), #2563eb); color:#ffffff; border:none; font-weight:700;
}}
.stButton > button[kind="primary"]:hover, .stButton > button[data-testid="stBaseButton-primary"]:hover {{
  background: linear-gradient(90deg, #5cc8fa, #3b82f6); color:#ffffff;
}}

/* ============================ Tabelas ============================ */
[data-testid="stDataFrame"] {{ font-family: 'JetBrains Mono', monospace; font-size: 12.5px; }}

/* ============================ Alertas ============================ */
.stAlert {{ border-radius: 10px; font-size: 13px; }}

/* ============================ Painel de decisão (efeitos) ============================ */
.decision-panel {{
  border-radius: 8px; padding: 14px 16px; margin-top: 10px;
  font-family: 'Manrope', sans-serif;
  transition: all .3s ease;
}}
.decision-panel .d-title {{
  font-size: 1.1em; font-weight: 700; margin-bottom: 4px;
}}
.decision-panel .d-sub {{
  font-size: 0.9em; opacity: 0.9;
}}

/* ── ENTRAR: pulso verde + glow ── */
.decision-enter {{
  background: linear-gradient(135deg, rgba(52,211,153,0.15), rgba(52,211,153,0.05));
  border: 1.5px solid rgba(52,211,153,0.6);
  box-shadow: 0 0 12px rgba(52,211,153,0.2);
  animation: glow-green 2s ease-in-out infinite;
}}
@keyframes glow-green {{
  0%, 100% {{ box-shadow: 0 0 12px rgba(52,211,153,0.2); }}
  50%      {{ box-shadow: 0 0 24px rgba(52,211,153,0.45), 0 0 48px rgba(52,211,153,0.15); }}
}}
.decision-enter .d-title {{ color: #34d399; }}

/* ── AVALIAR: pulso âmbar suave ── */
.decision-evaluate {{
  background: linear-gradient(135deg, rgba(251,191,36,0.12), rgba(251,191,36,0.04));
  border: 1.5px solid rgba(251,191,36,0.5);
  box-shadow: 0 0 8px rgba(251,191,36,0.15);
  animation: glow-amber 3s ease-in-out infinite;
}}
@keyframes glow-amber {{
  0%, 100% {{ box-shadow: 0 0 8px rgba(251,191,36,0.15); }}
  50%      {{ box-shadow: 0 0 18px rgba(251,191,36,0.35); }}
}}
.decision-evaluate .d-title {{ color: #fbbf24; }}

/* ── NÃO ENTRAR: estático, sem animação (não precisa chamar atenção) ── */
.decision-block {{
  background: rgba(239,83,80,0.08);
  border: 1px solid rgba(239,83,80,0.35);
  opacity: 0.75;
}}
.decision-block .d-title {{ color: #ef5350; }}

/* ── Flash de sinal novo ── */
@keyframes signal-flash {{
  0%   {{ background-color: rgba(56,189,248,0.25); }}
  100% {{ background-color: transparent; }}
}}
.signal-new {{
  animation: signal-flash 1.2s ease-out;
  border-radius: 8px;
}}

/* ── Tabela de trades ── */
.trade-table {{
  width: 100%;
  border-collapse: collapse;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}}
.trade-table th {{
  background: #141f36;
  color: var(--muted);
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.5px;
}}
.trade-table td {{
  padding: 6px 12px;
  border-bottom: 1px solid rgba(30,41,59,0.5);
}}
.trade-table tr:hover {{
  background: rgba(56,189,248,0.05);
}}
.trade-row-gain td {{
  color: var(--up);
}}
.trade-row-loss td {{
  color: var(--down);
}}
.trade-pnl-pos {{
  color: var(--up);
  font-weight: 600;
}}
.trade-pnl-neg {{
  color: var(--down);
  font-weight: 600;
}}
.trade-result-badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}}
.trade-result-badge.gain {{
  background: rgba(52,211,153,0.15);
  color: var(--up);
  border: 1px solid rgba(52,211,153,0.3);
}}
.trade-result-badge.loss {{
  background: rgba(251,113,133,0.15);
  color: var(--down);
  border: 1px solid rgba(251,113,133,0.3);
}}
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


def trade_table_html(df) -> str:
    """Gera tabela HTML estilizada de trades com gain/loss colorido."""
    if df.empty:
        return "<p style='color: var(--muted);'>Nenhum trade registrado.</p>"

    rows = ""
    for _, row in df.iterrows():
        pnl = float(row.get('Lucro ($)', row.get('profit', 0)))
        pnl_cls = "trade-pnl-pos" if pnl > 0 else "trade-pnl-neg"
        row_cls = "trade-row-gain" if pnl > 0 else "trade-row-loss"
        badge_cls = "gain" if pnl > 0 else "loss"
        badge_text = "✅ Gain" if pnl > 0 else "❌ Loss"

        cells = ""
        for col in df.columns:
            val = row[col]
            if col in ('Lucro ($)', 'profit'):
                cells += f'<td class="{pnl_cls}">${pnl:+,.2f}</td>'
            elif col == 'Resultado' or col == 'status':
                cells += f'<td><span class="trade-result-badge {badge_cls}">{badge_text}</span></td>'
            else:
                cells += f'<td>{val}</td>'
        rows += f'<tr class="{row_cls}">{cells}</tr>'

    headers = "".join(f"<th>{col}</th>" for col in df.columns)
    return f'<table class="trade-table"><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>'