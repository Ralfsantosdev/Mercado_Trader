"""
Backtest honesto dos sinais gerados pelo dashboard.

Como funciona:
- Entra no close do candle em que o sinal aparece (long=+1, short=-1).
- Sai por: stop loss / take profit em múltiplos de ATR, sinal contrário,
  ou fim do histórico.
- Custo: spread (pips * valor do pip) em cada operação — sem isso o backtest
  superestima o resultado (pegadinha clássica).
- Dimensionamento: risco fixo por operação (% da equity), posição calculada
  para que o SL custe exatamente esse risco — padrão de gestão profissional.
- NUNCA fecha no mesmo candle da entrada (não dá pra sair antes de entrar).
- Utiliza somente dados que existiam na época (sem look-ahead).

Métricas: retorno total, taxa de acerto, profit factor, expectativa por trade,
drawdown máximo, Sharpe diário. A leitura honesta: profit factor < 1.0 e
expectativa negativa significam que a estratégia PERDE para o spread.
"""

import numpy as np
import pandas as pd

PIP_SIZE = 0.0001  # maioria dos pares principais; JPY usa 0.01 — ajustar se preciso


def run_backtest(
    df: pd.DataFrame,
    spread_pips: float = 2.0,
    sl_atr: float = 2.0,
    tp_atr: float = 2.0,
    risk_per_trade_pct: float = 0.25,
    initial_capital: float = 10000.0,
    pip_size: float = PIP_SIZE,
    min_sl_pips: float = 10.0,
):
    data = df.copy().dropna(subset=["ATR", "Signal"])
    if len(data) < 2:
        raise ValueError("Dados insuficientes para backtest.")

    cash = initial_capital
    open_trade = None
    trades = []
    equity_curve = []

    for i in range(len(data)):
        row = data.iloc[i]
        ts = data.index[i]
        atr = float(row["ATR"])
        signal = int(row["Signal"])
        spread_cost = spread_pips * pip_size
        last_bar = i == len(data) - 1

        # --- gerencia posição aberta (entrada aconteceu no candle anterior) ---
        if open_trade is not None:
            entry_ts, entry_price, direction, size = open_trade
            sl_price = entry_price - direction * max(sl_atr * atr, min_sl_pips * pip_size)
            tp_price = entry_price + direction * max(tp_atr * atr, min_sl_pips * pip_size)
            high, low = float(row["High"]), float(row["Low"])

            exit_price, exit_reason = None, None
            if direction == 1:
                if low <= sl_price:
                    exit_price, exit_reason = sl_price, "Stop loss"
                elif high >= tp_price:
                    exit_price, exit_reason = tp_price, "Take profit"
            else:
                if high >= sl_price:
                    exit_price, exit_reason = sl_price, "Stop loss"
                elif low <= tp_price:
                    exit_price, exit_reason = tp_price, "Take profit"

            if exit_price is None and signal != 0 and signal != direction:
                exit_price, exit_reason = row["Close"], "Sinal contrário"
            if exit_price is None and last_bar:
                exit_price, exit_reason = row["Close"], "Fim do histórico"

            if exit_price is not None:
                pnl = (exit_price - entry_price) * direction * size - spread_cost * size
                cash += pnl
                ret_pct = pnl / (size * entry_price) * 100
                trades.append({
                    "Entrada": entry_ts,
                    "Saída": ts,
                    "Direção": "LONG" if direction == 1 else "SHORT",
                    "Preço entrada": round(entry_price, 5),
                    "Preço saída": round(exit_price, 5),
                    "Motivo saída": exit_reason,
                    "Tamanho": round(size, 2),
                    "PnL": round(pnl, 2),
                    "Retorno %": round(ret_pct, 3),
                    "Duração": (ts - entry_ts),
                })
                open_trade = None

        # --- abre nova posição no close deste candle ---
        if open_trade is None and signal != 0:
            sl_distance = max(sl_atr * atr, min_sl_pips * pip_size)
            if sl_distance > 0:
                size = (cash * risk_per_trade_pct / 100.0) / sl_distance
            else:
                size = 0.0
            open_trade = (ts, float(row["Close"]), signal, size)

        # --- equity marcada a mercado a cada candle (p/ drawdown correto) ---
        equity = cash
        if open_trade is not None:
            entry_ts, entry_price, direction, size = open_trade
            equity += (float(row["Close"]) - entry_price) * direction * size
        equity_curve.append((ts, equity))

    # fecha o que sobrar ao final do período
    if open_trade is not None:
        entry_ts, entry_price, direction, size = open_trade
        pnl = (float(data["Close"].iloc[-1]) - entry_price) * direction * size - spread_pips * pip_size * size
        cash += pnl
        trades.append({
            "Entrada": entry_ts,
            "Saída": data.index[-1],
            "Direção": "LONG" if direction == 1 else "SHORT",
            "Preço entrada": round(entry_price, 5),
            "Preço saída": round(float(data["Close"].iloc[-1]), 5),
            "Motivo saída": "Fim do histórico",
            "Tamanho": round(size, 2),
            "PnL": round(pnl, 2),
            "Retorno %": round(pnl / (size * entry_price) * 100, 3),
            "Duração": data.index[-1] - entry_ts,
        })

    return _build_result(trades, equity_curve, initial_capital, spread_pips, pip_size)


def _build_result(trades, equity_curve, initial_capital, spread_pips, pip_size=PIP_SIZE):
    trades_df = pd.DataFrame(trades)

    metrics = {
        "Capital inicial": initial_capital,
        "Capital final": None,
        "Retorno total %": None,
        "Nº de trades": len(trades),
        "Taxa de acerto %": None,
        "Profit factor": None,
        "Expectativa por trade": None,
        "Trade médio vencedor": None,
        "Trade médio perdedor": None,
        "Melhor trade": None,
        "Pior trade": None,
        "Drawdown máximo %": None,
        "Sharpe (diário)": None,
        "Spread usado (pips)": spread_pips,
        "Pip size": pip_size,
    }

    if not trades_df.empty:
        wins = trades_df["PnL"] > 0
        gross_win = trades_df.loc[wins, "PnL"].sum()
        gross_loss = abs(trades_df.loc[~wins, "PnL"].sum())
        metrics["Retorno total %"] = round((trades_df["PnL"].sum() / initial_capital) * 100, 2)
        metrics["Taxa de acerto %"] = round(wins.mean() * 100, 1)
        metrics["Profit factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
        metrics["Expectativa por trade"] = round(trades_df["PnL"].mean(), 2)
        metrics["Trade médio vencedor"] = round(trades_df.loc[wins, "PnL"].mean(), 2)
        metrics["Trade médio perdedor"] = round(trades_df.loc[~wins, "PnL"].mean(), 2)
        metrics["Melhor trade"] = round(trades_df["PnL"].max(), 2)
        metrics["Pior trade"] = round(trades_df["PnL"].min(), 2)

    # equity curve -> drawdown e Sharpe
    eq = pd.Series(dict(equity_curve), dtype=float).sort_index()
    if len(eq) > 1:
        final = float(eq.iloc[-1])
        metrics["Capital final"] = round(final, 2)
        peak = eq.cummax()
        drawdown = (eq - peak) / peak
        metrics["Drawdown máximo %"] = round(-drawdown.min() * 100, 2)

        daily = eq.resample("D").last().dropna()
        rets = daily.pct_change().dropna()
        if len(rets) > 1 and rets.std() > 0:
            metrics["Sharpe (diário)"] = round(rets.mean() / rets.std() * np.sqrt(365), 2)

    return trades_df, metrics, eq


def summarize(metrics: dict) -> str:
    """Leitura honesta do resultado em uma linha."""
    pf = metrics.get("Profit factor")
    exp = metrics.get("Expectativa por trade")
    n = metrics.get("Nº de trades", 0)
    if n < 20:
        return "Amostra pequena demais para concluir qualquer coisa."
    if pf is None:
        return "Nenhum trade lucrativo — a estratégia perdeu tudo para o spread."
    if pf >= 1.2 and exp is not None and exp > 0:
        return f"Resultado positivo (profit factor {pf}). Repita em mais períodos e pares antes de acreditar."
    if pf < 1.0:
        return "Profit factor < 1.0: os custos (spread) comem o resultado. Não operável assim."
    return "Empate técnico: lucro bruto próximo do custo do spread. Resultado não é conclusivo."
