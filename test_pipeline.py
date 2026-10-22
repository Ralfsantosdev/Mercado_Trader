"""Teste de sanidade com dados sintéticos - não depende de rede."""
import numpy as np
import pandas as pd
from indicators import add_all_indicators, rsi
from model import generate_rule_based_signals, volume_is_meaningful, train_ml_model, predict_latest
from backtest import run_backtest, summarize

np.random.seed(42)
n = 500
dates = pd.date_range("2024-01-01", periods=n, freq="h")

# gera um preço sintético tipo random-walk com leve tendência
returns = np.random.normal(0.0001, 0.003, n)
close = 1.10 * np.exp(np.cumsum(returns))
high = close * (1 + np.abs(np.random.normal(0, 0.001, n)))
low = close * (1 - np.abs(np.random.normal(0, 0.001, n)))
open_ = close * (1 + np.random.normal(0, 0.0005, n))
volume = np.random.randint(100, 1000, n)

df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=dates)

print("1) Testando indicadores...")
df["Return"] = df["Close"].pct_change()
df["Direction"] = (df["Return"].shift(-1) > 0).astype(int)
df = add_all_indicators(df)
assert df["RSI"].dropna().between(0, 100).all(), "RSI fora do range 0-100"
assert not df["SMA_200"].dropna().empty, "SMA_200 não calculou"
print("   OK - RSI no range correto, médias calculadas")

print("2) Testando geração de sinais por regra...")
df = generate_rule_based_signals(df)
print(f"   OK - Sinais gerados: {(df['Signal']==1).sum()} compras, {(df['Signal']==-1).sum()} vendas")

print("3) Testando treino do modelo com split temporal...")
model, metrics, (X_test, y_test) = train_ml_model(df)
print(f"   OK - Acurácia fora da amostra: {metrics['accuracy']*100:.1f}%")
print(f"   (esperado ~45-55% com dados aleatórios sintéticos - isso é o comportamento CORRETO,")
print(f"   um valor muito acima disso indicaria vazamento de dados)")

print("4) Testando previsão da última linha...")
pred, margin, p = predict_latest(model, df)
print(f"   OK - Previsão: {'Subir' if pred==1 else 'Cair'} (margem {margin*100:.1f}%, P(subir)={p*100:.1f}%)")
assert 0 <= margin <= 0.5, "margem de confiança fora do intervalo válido"

print("5) Confirmando que a linha usada na previsão está fora do treino (sem vazamento)...")
last_valid_idx = df.dropna(subset=["SMA_9","SMA_21","SMA_200","RSI","MACD","MACD_Hist","ATR","Volatility"]).index[-1]
assert last_valid_idx in X_test.index or last_valid_idx >= X_test.index[0], "possível vazamento"
print("   OK - última linha está no período de teste (após o corte temporal), não no treino")

print("6) Testando sinal com volume zerado (caso Yahoo forex)...")
df_novol = df.copy()
df_novol["Volume"] = 0
assert volume_is_meaningful(df_novol) is False, "deveria detectar volume inútil"
df_novol = add_all_indicators(df_novol)
df_novol = generate_rule_based_signals(df_novol, rsi_overbought=90, rsi_oversold=10, use_volume=True, use_cross=False)
n_buy = (df_novol["Signal"] == 1).sum()
n_sell = (df_novol["Signal"] == -1).sum()
print(f"   OK - volume zerado → confirmação por ATR, sinais gerados: {n_buy} compras, {n_sell} vendas")
assert n_buy > 0 or n_sell > 0, "sinal morreu com volume zerado (bug não corrigido)"

print("7) Testando backtest com spread...")
trades_df, metrics, equity = run_backtest(df, spread_pips=2.0, sl_atr=2.0, tp_atr=2.0)
print(f"   OK - {metrics['Nº de trades']} trades, retorno {metrics['Retorno total %']}%, "
      f"profit factor {metrics['Profit factor']}, drawdown {metrics['Drawdown máximo %']}%")
print(f"   Verdict: {summarize(metrics)}")
if not trades_df.empty:
    assert "PnL" in trades_df.columns and "Motivo saída" in trades_df.columns
    assert trades_df["Motivo saída"].isin(["Stop loss", "Take profit", "Sinal contrário", "Fim do histórico"]).all()
    print("   OK - colunas e motivos de saída válidos")

print("8) Testando volatilidade precisa (Parkinson/GK/Yang-Zhang + regime)...")
vol_cols = ["Vol_Parkinson", "Vol_GK", "Vol_YZ", "Vol_Realized", "Vol_Precise",
            "Vol_Precise_Pct", "Vol_Percentile", "Vol_Regime", "Vol_Stop_Mult", "Efficiency_Ratio"]
for col in vol_cols:
    assert col in df.columns, f"coluna {col} ausente"
assert df["Vol_Percentile"].dropna().between(0, 100).all(), "percentil fora de 0-100"
assert df["Vol_Stop_Mult"].dropna().between(1.0, 1.6).all(), "multiplicador fora de 1.0-1.6"
assert df["Efficiency_Ratio"].dropna().between(0, 1).all(), "ER fora de 0-1"
_reg = df.loc[df["Vol_Percentile"].notna(), "Vol_Regime"]
assert _reg.isin(["BAIXA", "NORMAL", "ALTA", "EXTREMA"]).all(), "regime inválido"
assert df.loc[df["Vol_Percentile"].isna(), "Vol_Regime"].eq("").all(), "regime deveria ser vazio no warm-up"
from indicators import confirmation_score
_score, _checks = confirmation_score(df, 1)
assert len(_checks) == 11, f"checklist deveria ter 11 itens, tem {len(_checks)}"
print(f"   OK - colunas de vol presentes, regimes válidos, checklist com {len(_checks)} checks")

print("9) Testando gate global de volatilidade (bloqueio de rajada/chop)...")
_n_normal = int((df["Signal"] != 0).sum())
assert _n_normal > 0, "cenário base sem sinal (seed mudou?)"
df_ext = df.copy()
df_ext["Vol_Percentile"] = 99.0
df_ext["Vol_Regime"] = "EXTREMA"
df_ext["Vol_Stop_Mult"] = 1.6
df_ext["Efficiency_Ratio"] = 0.5  # isola o gate de percentil
df_ext = generate_rule_based_signals(df_ext)
assert (df_ext["Signal"] != 0).sum() == 0, "gate de volatilidade extrema não zerou os sinais"
if int(df["Signal"].iloc[-1]) != 0:
    assert df_ext.attrs.get("vol_block_msg"), "mensagem de bloqueio por volatilidade ausente"

df_chop = df.copy()
df_chop["Vol_Percentile"] = 50.0
df_chop["Efficiency_Ratio"] = 0.05  # lateral pura
df_chop = generate_rule_based_signals(df_chop)
assert (df_chop["Signal"] != 0).sum() == 0, "gate de chop (ER baixo) não zerou os sinais"
print(f"   OK - { _n_normal} sinais normais → 0 com vol extrema e 0 com chop")

print("10) Testando stop adaptativo no PLS (rajada alarga, regime extremo bloqueia)...")
import numpy as _np
import pandas as _pd
from indicators import add_all_indicators as _add_ind
from pullback_liquidity_strategy import PullbackLiquidityConfig, build_signal as _pls_signal

def _make_pls_df(seed=1):
    """Cenário engenheirado: alta → consolidação → sweep + gatilho de compra."""
    rng = _np.random.default_rng(seed)
    base, n_a, n_b = 100.0, 400, 30
    drift = _np.exp(_np.cumsum(rng.normal(0.0006, 0.0002, n_a)))
    closes_a = base * drift / drift[-1]
    o_a = _np.concatenate([[closes_a[0]], closes_a[:-1]])
    o_b = _np.empty(n_b); c_b = _np.empty(n_b)
    for k in range(n_b):
        o_b[k] = base - 0.02 if k % 2 else base + 0.02
        c_b[k] = base + 0.02 if k % 2 else base - 0.02
    h_b = _np.full(n_b, base + 0.03)
    l_b = _np.full(n_b, base - 0.03)
    prior_low = l_b[-20:].min()
    o = _np.concatenate([o_a, o_b, [base - 0.01, base + 0.04]])
    c = _np.concatenate([closes_a, c_b, [base + 0.036, base + 0.04]])
    h = _np.concatenate([closes_a * 1.0003, h_b, [base + 0.045, base + 0.049]])
    l = _np.concatenate([closes_a * 0.9997, l_b, [prior_low - 0.006, base + 0.03]])
    idx = _pd.date_range("2026-01-01", periods=len(o), freq="5min", tz="UTC")
    out = _pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 100.0}, index=idx)
    out = _add_ind(out)
    out["Market_Zone"] = "DESEQUILIBRIO"
    n = len(out); i = _np.arange(n)
    out["EMA_20"] = base + 0.5 * i / n   # EMAs controladas (teste isola a lógica de vol)
    out["EMA_50"] = base
    out["EMA_200"] = base - 10.0
    return out

def _with_vol(frame, pct, mult, er=0.5):
    d = frame.copy()
    d["Vol_Percentile"] = pct
    d["Vol_Stop_Mult"] = mult
    d["Efficiency_Ratio"] = er
    d["Vol_Regime"] = "EXTREMA" if pct > 90 else ("ALTA" if pct > 75 else "NORMAL")
    return d

_pls_df = _make_pls_df()
_cfg = PullbackLiquidityConfig()
sig_normal = _pls_signal(_with_vol(_pls_df, 50, 1.0), cfg=_cfg)
assert sig_normal is not None, "cenário PLS deveria gerar sinal com vol normal"
sig_adapt = _pls_signal(_with_vol(_pls_df, 80, 1.6), cfg=_cfg)
assert sig_adapt is not None, "sinal sumiu com vol ALTA (só deveria alargar o stop)"
assert sig_adapt.risk_price > sig_normal.risk_price, "stop não alargou no regime ALTA"
assert sig_adapt.stop_mult > 1.0 and sig_adapt.vol_percentile == 80
assert _pls_signal(_with_vol(_pls_df, 95, 1.6), cfg=_cfg) is None, "regime EXTREMA não bloqueou"
assert _pls_signal(_with_vol(_pls_df, 50, 1.0, er=0.05), cfg=_cfg) is None, "chop (ER<0.15) não bloqueou"
print(f"   OK - risco {sig_normal.risk_price:.5f} → {sig_adapt.risk_price:.5f} (×{sig_adapt.stop_mult:.2f}); "
      "EXTREMA e chop bloqueados")

print("11) Testando stop adaptativo no backtest e no executor...")
from executor import stop_distances as _stop_distances
_sl1, _tp1 = _stop_distances(1.0, 2.0, 3.0, vol_mult=1.0)
_sl2, _tp2 = _stop_distances(1.0, 2.0, 3.0, vol_mult=1.6)
assert abs(_sl2 - 3.2) < 1e-9 and abs(_sl1 - 2.0) < 1e-9, "stop_distances ignorou vol_mult"
assert abs(_tp2 - 4.8) < 1e-9, "TP em ATR não escalou com vol_mult"

def _micro_backtest(mult):
    """Preço cai 2.5×ATR (mata stop em vol normal) e depois sobe 4×ATR (bate TP)."""
    n2, A, C = 5, 1.0, 100.0
    d = _pd.DataFrame({
        "Open": [C, C, C - 1.0, C, C],
        "High": [C + 2 * A, C + 0.5 * A, C + 4 * A, C, C],
        "Low": [C - 0.1 * A, C - 2.5 * A, C - 0.1 * A, C, C],
        "Close": [C, C - 1.0 * A, C + 2.0 * A, C, C],
        "Volume": [10] * n2,
        "ATR": [A] * n2,
        "Signal": [1, 0, 0, 0, 0],
        "Vol_Stop_Mult": [mult] * n2,
        "Vol_Regime": ["ALTA" if mult > 1 else "NORMAL"] * n2,
    }, index=_pd.date_range("2026-01-01", periods=n2, freq="h"))
    trades, m, _eq = run_backtest(d, spread_pips=0.0, sl_atr=2.0, tp_atr=2.0, vol_stop=True)
    return trades.iloc[0]["Motivo saída"]

assert _micro_backtest(1.0) == "Stop loss", "vol normal deveria bater no stop (2.5 ATR > 2.0 ATR)"
assert _micro_backtest(1.6) == "Take profit", "vol ALTA deveria sobreviver à queda e bater no TP"
print("   OK - ×1.0 morre no stop; ×1.6 alarga, sobrevive e alcança o TP")

print("\n✅ TODOS OS TESTES PASSARAM")
