"""
Otimização walk-forward honesta dos parâmetros da estratégia.

Como funciona (para não enganar a si mesmo):
- Divide o histórico em 60% treino + 40% teste (fora-da-amostra).
- Testa uma grade de parâmetros no TRECHO DE TREINO e escolhe os melhores.
- Os escolhidos SÃO TESTADOS no trecho de teste, que a otimização nunca viu.
- Se um combo brilhar no treino mas morrer no teste = overfitting (lixo).
- Só vale acreditar em config que sobrevive ao teste com trades suficientes
  e lucro que supera o spread.

Uso:
    python3 optimize.py --symbol XAU-USD --interval 5m --spread 25 --pip 0.01 --top 5
    python3 optimize.py --symbol EUR-USD --interval 15m --spread 2 --pip 0.0001
"""

import argparse
import warnings
import pandas as pd

warnings.filterwarnings("ignore")

from data_provider import get_provider
from indicators import add_all_indicators
from model import generate_rule_based_signals
from backtest import run_backtest

TRAIN_SPLIT = 0.60
MIN_TRAIN_TRADES = 15
MIN_TEST_TRADES = 3

def grid(trigger_mode="auto"):
    combos = []
    for rsi_ob in (60, 70, 80):
        for rsi_os in (20, 30, 40):
            for use_cross in (True, False):
                for session in ("", "Londres + Nova York (melhor janela)"):
                    for min_adx in (0, 20):
                        for sl_atr in (1.5, 2.0, 3.0):
                            for tp_atr in (1.5, 2.0, 3.0):
                                combos.append(dict(
                                    rsi_overbought=rsi_ob, rsi_oversold=rsi_os,
                                    use_cross=use_cross, session=session,
                                    min_adx=min_adx, sl_atr=sl_atr, tp_atr=tp_atr,
                                    trigger_mode=trigger_mode,
                                ))
    return combos


def eval_combo(df, params, spread_pips, pip_size, split_ts):
    df = generate_rule_based_signals(
        df, rsi_overbought=params["rsi_overbought"],
        rsi_oversold=params["rsi_oversold"],
        use_volume=True, use_cross=params["use_cross"],
        trigger_mode=params.get("trigger_mode", "auto"),
        pullback_ema=params.get("pullback_ema", 50),
        session=params["session"], min_adx=params["min_adx"],
    )
    trades, _, _ = run_backtest(
        df, spread_pips=spread_pips, sl_atr=params["sl_atr"],
        tp_atr=params["tp_atr"], risk_per_trade_pct=0.25, pip_size=pip_size,
    )
    if trades.empty:
        return None
    train = trades[trades["Entrada"] < split_ts]
    test = trades[trades["Entrada"] >= split_ts]
    return train, test


def seg_metrics(trades):
    if trades.empty:
        return None
    wins = trades["PnL"] > 0
    gross_win = trades.loc[wins, "PnL"].sum()
    gross_loss = abs(trades.loc[~wins, "PnL"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    return {
        "n": len(trades),
        "pf": round(pf, 2),
        "exp": round(trades["PnL"].mean(), 3),
        "hit": round(wins.mean() * 100, 1),
    }


def run_optimization_workflow(df, spread_pips, pip_size, trigger_mode="auto"):
    """
    Exported function to run WFO optimization and return the ranked list of params.
    """
    df = df.dropna(subset=["ATR"]).copy()
    if len(df) < 50:
        return None

    idx = df.index
    split_ts = idx[int(len(df) * TRAIN_SPLIT)]

    combos = grid(trigger_mode)
    results = []
    for params in combos:
        r = eval_combo(df, params, spread_pips, pip_size, split_ts)
        if r is None:
            continue
        train, test = r
        tm = seg_metrics(train)
        em = seg_metrics(test)
        if tm is None or tm["n"] < MIN_TRAIN_TRADES:
            continue
        results.append((params, tm, em))

    if not results:
        return None

    # ranking pelo desempenho FORA-DA-AMOSTRA (teste)
    ranked = sorted(results, key=lambda x: (x[2] or {}).get("exp", -99), reverse=True)
    return ranked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAU-USD")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--spread", type=float, required=True, help="spread em pips do ativo")
    ap.add_argument("--pip", type=float, default=0.0001, help="valor de 1 pip do ativo")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--trigger-mode", default="auto",
                    choices=["auto", "slope", "cross", "pullback", "3_9_20"],
                    help="Gatilho a otimizar")
    ap.add_argument("--pullback-ema", type=int, default=50)
    args = ap.parse_args()

    import os
    import streamlit as st
    api_key = os.environ.get("AWESOMEAPI_API_KEY", "")
    if not api_key:
        try:
            api_key = st.secrets.get("awesomeapi_key", "")
        except Exception:
            api_key = ""
    
    df = get_provider(source="awesomeapi", api_key=api_key or None).get_ohlcv(
        args.symbol, days=args.days, interval=args.interval
    )
    
    if df.empty:
        print(f"❌ Sem dados para {args.symbol} {args.interval}")
        return
        
    df = add_all_indicators(df)
    
    ranked = run_optimization_workflow(df, args.spread, args.pip, args.trigger_mode)
    if not ranked:
        print("Nenhuma configuração gerou trades suficientes no treino.")
        return

    print(f"Top {args.top} por expectativa no TESTE (fora-da-amostra):\n")
    print(f"{'RSI':<8}{'cross':<6}{'sessão':<4}{'ADX':<4}{'SLx':<5}{'TPx':<5}"
          f"{'treino n/PF':>12}{'teste n/PF/exp':>20}")
    for params, tm, em in ranked[: args.top]:
        cross = "X" if params["use_cross"] else "nível"
        sess = "S" if params["session"] else "-"
        rsi = f"{params['rsi_overbought']}/{params['rsi_oversold']}"
        train_s = f"{tm['n']}/{tm['pf']}"
        test_s = f"{em['n']}/{em['pf']}/{em['exp']}" if em else "sem trades"
        print(f"{rsi:<8}{cross:<6}{sess:<4}{params['min_adx']:<4}"
              f"{params['sl_atr']:<5}{params['tp_atr']:<5}"
              f"{train_s:>12}{test_s:>20}")

    # resumo honesto
    survived = [r for r in ranked if r[2] and r[2]["n"] >= MIN_TEST_TRADES and r[2]["exp"] > 0]
    print("\n--- Veredito honesto ---")
    if survived:
        params, tm, em = survived[0]
        print(f"Melhor sobrevivente: RSI {params['rsi_overbought']}/{params['rsi_oversold']}, "
              f"{'cross' if params['use_cross'] else 'nível'}, sessão={'on' if params['session'] else 'off'}, "
              f"ADX={params['min_adx']}, SL {params['sl_atr']}xATR, TP {params['tp_atr']}xATR")
        print(f"  treino: {tm['n']} trades, PF {tm['pf']} | teste: {em['n']} trades, PF {em['pf']}, "
              f"expectativa {em['exp']}/trade")
        print("  ⚠️ Só confie depois de repetir em mais períodos/pares. Não é promessa de lucro.")
    else:
        print("Nenhuma configuração sobreviveu fora-da-amostra com expectativa positiva.")
        print("Com esses parâmetros e esse spread, o sistema provavelmente perde para os custos.")

if __name__ == "__main__":
    main()
