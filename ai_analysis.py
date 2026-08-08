"""
Análise de probabilidade e risco com IA — combina estatística local honesta
(Monte Carlo, VaR, probabilidade de TP/SL) com um parecer narrativo de um LLM
(OpenAI-compatible) que interpreta esses números sem inventar valores.

Aviso honesto (mesmo padrão do resto do app): nada aqui prevê o mercado com
certeza. O que estas métricas fazem é quantificar o risco da operação na
configuração atual (SL/TP/spread) — se o trade é "cara ou coroa", o Monte Carlo
mostra isso sem maquiagem.

Dependências: numpy/pandas (já usadas). O LLM usa o pacote `openai` se
instalado (qualquer endpoint OpenAI-compatible); sem chave de API o parecer
narrativo é desligado, mas toda a estatística continua funcionando offline.
"""

import math

import numpy as np
import pandas as pd

RNG_SEED = 42


# ---------------------------------------------------------------------------
# Probabilidade de tocar o TP antes do SL (formula de barreira absorvente)
# ---------------------------------------------------------------------------
def _prob_hit_tp_first(drift, variance, tp_dist, sl_dist):
    """
    P(movimento Browniano com drift 'drift' e variancia 'variance' por candle
    toca +tp_dist antes de -sl_dist), partindo de 0.

    Formula classica (Ross, Probability Models):
        P(hit +a antes de -b) = (1 - exp(-2*mu*b/V)) / (1 - exp(-2*mu*(a+b)/V))
    com mu = drift, V = variancia. Para mu <= 0 usamos a simetria da reflexao.
    Tudo calculado com np.expm1 -> estável mesmo com drift ~ 0 (mercado lateral).
    """
    if tp_dist <= 0 or sl_dist <= 0:
        return 0.0 if tp_dist <= 0 else 1.0
    if variance <= 1e-15:
        return 1.0 if drift > 0 else 0.0

    z = 2.0 * drift / variance
    if abs(z) < 1e-12:
        return sl_dist / (tp_dist + sl_dist)

    if z > 0:
        num = -np.expm1(-z * sl_dist)
        den = -np.expm1(-z * (tp_dist + sl_dist))
        if den <= 0:
            return 1.0
        return float(min(1.0, num / den))
    # drift negativo (contra a posicao): calcula P(hit SL) no processo refletido
    num = -np.expm1(z * tp_dist)
    den = -np.expm1(z * (tp_dist + sl_dist))
    if den <= 0:
        return 0.0
    return float(max(0.0, 1.0 - num / den))


# ---------------------------------------------------------------------------
# Monte Carlo por bootstrap dos retornos reais
# ---------------------------------------------------------------------------
def monte_carlo(
    df,
    direction,
    sl_atr,
    tp_atr,
    spread_pips,
    pip_size,
    horizon=30,
    n_paths=2000,
    seed=RNG_SEED,
):
    """
    Simula n_paths caminhos de 'horizon' candles usando bootstrap (com reposicao)
    dos retornos reais observados no historico. Para cada caminho verifica se o
    preco toca o TP (a + spread/2) ou o SL (b - spread/2) primeiro.

    Retorna dict com P(TP), P(SL), P(fim do horizonte), distribuicao de R
    (retorno / risco em multiplos do SL), expectativa e percentis.
    """
    r = df["Return"].dropna().values
    if len(r) < 30:
        raise ValueError("Histórico curto demais para Monte Carlo (mín. 30 candles).")

    price = float(df["Close"].iloc[-1])
    atr = float(df["ATR"].iloc[-1]) if pd.notna(df["ATR"].iloc[-1]) else float(df["ATR"].dropna().iloc[-1])
    spread = spread_pips * pip_size
    s_half = spread / 2.0

    tp_dist = tp_atr * atr
    sl_dist = sl_atr * atr

    rng = np.random.default_rng(seed)
    samples = rng.choice(r, size=(n_paths, horizon), replace=True)
    paths = price * np.cumprod(1.0 + samples, axis=1)

    if direction == 1:
        tp_level = price + tp_dist + s_half
        sl_level = price - sl_dist + s_half
    else:
        tp_level = price - tp_dist - s_half
        sl_level = price + sl_dist - s_half

    # acha o primeiro candle em que o caminho cruza uma barreira
    hit_tp = paths >= tp_level
    hit_sl = paths <= sl_level

    r_results = np.empty(n_paths)
    outcomes = np.empty(n_paths, dtype=int)  # 1=TP, -1=SL, 0=horizonte
    for i in range(n_paths):
        first_tp = np.flatnonzero(hit_tp[i])
        first_sl = np.flatnonzero(hit_sl[i])
        t_tp = first_tp[0] if len(first_tp) else horizon
        t_sl = first_sl[0] if len(first_sl) else horizon
        if t_tp < t_sl:
            outcomes[i] = 1
            pnl = tp_dist
        elif t_sl < t_tp:
            outcomes[i] = -1
            pnl = -sl_dist
        else:
            outcomes[i] = 0
            exit_price = paths[i, -1]
            pnl = (exit_price - price) * direction - spread
        r_results[i] = pnl / sl_dist if sl_dist > 0 else (-1.0 if outcomes[i] != 1 else 1.0)

    p_tp = float((outcomes == 1).mean())
    p_sl = float((outcomes == -1).mean())
    p_flat = float((outcomes == 0).mean())

    r_flat = r_results[outcomes == 0] if (outcomes == 0).any() else r_results
    return {
        "n_paths": n_paths,
        "horizon": horizon,
        "p_tp": p_tp,
        "p_sl": p_sl,
        "p_flat": p_flat,
        "expected_r": float(np.mean(r_results)),
        "median_r": float(np.median(r_results)),
        "p_r_positive": float((r_results > 0).mean()),
        "percentiles_r": {
            p: float(np.percentile(r_results, p)) for p in (5, 25, 50, 75, 95)
        },
        "r_flat_samples": r_flat,
        "sl_dist": sl_dist,
        "tp_dist": tp_dist,
        "spread": spread,
    }


# ---------------------------------------------------------------------------
# Estatistica de mercado (tudo que alimenta o LLM e os paineis)
# ---------------------------------------------------------------------------
def statistical_analysis(
    df,
    direction,
    sl_atr,
    tp_atr,
    spread_pips,
    pip_size,
    ml_p=None,
    ml_pred=None,
    horizon=30,
):
    price = float(df["Close"].iloc[-1])
    ret = df["Return"].dropna()
    if len(ret) < 30:
        raise ValueError("Histórico curto demais para análise de risco (mín. 30 candles).")

    mu = float(ret.mean())
    sigma = float(ret.std())
    # converte para UNIDADE DE PREÇO (as distâncias TP/SL estão em preço)
    drift_price = mu * price
    variance_price = (sigma * price) ** 2
    if variance_price <= 1e-15:
        variance_price = 1e-15

    atr = float(df["ATR"].iloc[-1]) if pd.notna(df["ATR"].iloc[-1]) else float(df["ATR"].dropna().iloc[-1])
    atr_ma20 = float(df["ATR"].rolling(20).mean().iloc[-1]) or atr
    adx = float(df["ADX"].iloc[-1]) if pd.notna(df["ADX"].iloc[-1]) else 0.0
    rsi = float(df["RSI"].iloc[-1])
    macd_hist = float(df["MACD_Hist"].iloc[-1])
    bb_pos = float(df["BB_Position"].iloc[-1]) if pd.notna(df["BB_Position"].iloc[-1]) else 0.0
    sma9 = float(df["SMA_9"].iloc[-1])
    sma21 = float(df["SMA_21"].iloc[-1])
    sma200 = float(df["SMA_200"].iloc[-1])

    spread = spread_pips * pip_size
    tp_dist = tp_atr * atr
    sl_dist = sl_atr * atr

    # barreiras efetivas a partir do preço MID (entrada no ask/bid paga o spread)
    a_eff = tp_dist + spread / 2.0
    b_eff = sl_dist - spread / 2.0

    drift_pos = direction * drift_price  # drift no sentido da posição

    if b_eff > 0 and a_eff > 0:
        p_tp_formula = _prob_hit_tp_first(drift_pos, variance_price, a_eff, b_eff)
    else:
        p_tp_formula = 0.0
    p_sl_formula = 1.0 - p_tp_formula

    # frequência histórica da direção (janela móvel dos últimos candles)
    win = ret.tail(min(len(ret), 120))
    p_hist_dir = float((win * direction > 0).mean())

    # VaR por candle (a partir da distribuição empírica)
    var95 = -float(np.percentile(ret, 5)) * 100.0
    var99 = -float(np.percentile(ret, 1)) * 100.0

    # movimento esperado no horizonte (aproximação √n do passeio aleatório)
    expected_move = atr * math.sqrt(horizon)
    expected_move_pct = sigma * math.sqrt(horizon) * 100.0

    atr_ratio = atr / atr_ma20 if atr_ma20 else 1.0
    if adx >= 25:
        trend_label = "Tendência forte"
    elif adx >= 20:
        trend_label = "Tendência moderada"
    else:
        trend_label = "Lateral (range)"

    if atr_ratio > 1.25:
        vol_label = "Volatilidade em alta"
    elif atr_ratio < 0.8:
        vol_label = "Volatilidade em baixa"
    else:
        vol_label = "Volatilidade normal"

    return {
        "price": price,
        "direction": int(direction),
        "mu_candle": mu,
        "sigma_candle": sigma,
        "vol_candle_pct": sigma * 100.0,
        "atr": atr,
        "atr_ratio": atr_ratio,
        "vol_label": vol_label,
        "adx": adx,
        "trend_label": trend_label,
        "trend_align": sma9 > sma21,
        "close_vs_sma200": price > sma200,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "bb_pos": bb_pos,
        "sma9": sma9,
        "sma21": sma21,
        "sma200": sma200,
        "p_tp_formula": p_tp_formula,
        "p_sl_formula": p_sl_formula,
        "p_hist_dir": p_hist_dir,
        "ml_p": ml_p,
        "ml_pred": ml_pred,
        "var95_pct": var95,
        "var99_pct": var99,
        "expected_move": expected_move,
        "expected_move_pct": expected_move_pct,
        "tp_dist": tp_dist,
        "sl_dist": sl_dist,
        "a_eff": a_eff,
        "b_eff": b_eff,
        "spread": spread,
    }


def conviction_score(stats, mc):
    """
    Score de convicção (0-100), heurística transparente:
      40% confiança do ML no sentido da operação
      30% frequência histórica da direção
      30% probabilidade de TP no Monte Carlo
    Cada p é P(não errar). Valores próximos de 50 = cara ou coroa.
    """
    p_ml = None
    if stats["ml_p"] is not None and stats["ml_pred"] is not None:
        p_ml = stats["ml_p"] if stats["direction"] == stats["ml_pred"] else (1.0 - stats["ml_p"])

    p_hist = stats["p_hist_dir"]
    p_tp = mc["p_tp"]

    if p_ml is None:
        score = 100.0 * (0.5 * p_hist + 0.5 * p_tp)
        weights = {"freq. histórica": 0.5, "Monte Carlo TP": 0.5}
    else:
        score = 100.0 * (0.4 * p_ml + 0.3 * p_hist + 0.3 * p_tp)
        weights = {"confiança ML": 0.4, "freq. histórica": 0.3, "Monte Carlo TP": 0.3}
    return score, weights


# ---------------------------------------------------------------------------
# Parecer narrativo do LLM (OpenAI-compatible). DESLIGADO por padrão.
# ---------------------------------------------------------------------------
def llm_parecer(stats, mc, score, config):
    """
    O parecer narrativo foi desligado por decisão do usuário (consumia tokens).
    Qualquer chamada devolve None — a estatística local continua 100% offline.
    Para religar, reintegre a chamada à API por sua conta e risco.
    """
    return None


# ---------------------------------------------------------------------------
# Orquestra tudo
# ---------------------------------------------------------------------------
def run_analysis(
    df,
    direction,
    sl_atr,
    tp_atr,
    spread_pips,
    pip_size,
    ml_p=None,
    ml_pred=None,
    horizon=30,
    n_paths=2000,
    seed=RNG_SEED,
    llm_config=None,
):
    stats = statistical_analysis(
        df, direction, sl_atr, tp_atr, spread_pips, pip_size,
        ml_p=ml_p, ml_pred=ml_pred, horizon=horizon,
    )
    mc = monte_carlo(
        df, direction, sl_atr, tp_atr, spread_pips, pip_size,
        horizon=horizon, n_paths=n_paths, seed=seed,
    )
    score, weights = conviction_score(stats, mc)
    parecer = llm_parecer(stats, mc, score, llm_config)
    return {"stats": stats, "mc": mc, "score": score, "weights": weights, "parecer": parecer}
