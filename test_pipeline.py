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

print("\n✅ TODOS OS TESTES PASSARAM")
