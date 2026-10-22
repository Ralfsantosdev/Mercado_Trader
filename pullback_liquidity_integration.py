# INTEGRAÇÃO MÍNIMA COM O PROJETO EXISTENTE
#
# Arquivos citados no seu README:
# - model.py      -> geração do sinal
# - executor.py   -> risco / tamanho da posição / envio da ordem
# - alerts.py     -> Telegram / cron
# - backtest.py   -> deve consumir o MESMO sinal e o MESMO SL/TP
#
# Ajuste imports conforme a estrutura real do seu projeto.

from pullback_liquidity_strategy import (
    PullbackLiquidityConfig,
    build_signal,
    position_size_by_risk,
    risk_guard,
)

CFG = PullbackLiquidityConfig(
    adx_min=20.0,
    pullback_tolerance_atr=0.15,
    liquidity_lookback=20,
    min_body_fraction=0.35,
    stop_buffer_atr=0.10,
    min_stop_atr=0.80,
    max_stop_atr=1.50,
    rr_target=2.0,
    min_rr_to_liquidity=1.50,
    risk_per_trade_pct=0.25,
    daily_loss_limit_pct=1.00,   # TROQUE pelo limite diário que você já definiu.
    max_consecutive_losses=3,
    cooldown_bars=2,
)


# ----------------------------------------------------------------------
# model.py
# ----------------------------------------------------------------------

def pullback_liquidity_model(df):
    """
    Retorno compatível com um pipeline simples.
    O candle de sinal é fechado (-2 em feed ao vivo).
    """
    signal = build_signal(df, cfg=CFG, use_closed_candle=True)

    if signal is None:
        return {
            "Signal": 0,
            "Suggested_SL": None,
            "Suggested_TP": None,
            "Strategy": "pullback_liquidity",
            "Reason": "no_valid_setup",
        }

    return {
        "Signal": 1 if signal.side == "buy" else -1,
        "Side": signal.side,
        "Entry": signal.entry,
        "Suggested_SL": signal.stop,
        "Suggested_TP": signal.take,
        "Risk_Price": signal.risk_price,
        "RR": signal.rr,
        "ATR": signal.atr,
        "ADX": signal.adx,
        "Liquidity_Level": signal.liquidity_level,
        "Sweep_Extreme": signal.sweep_extreme,
        "Strategy": "pullback_liquidity",
        "Reason": signal.reason,
    }


# ----------------------------------------------------------------------
# executor.py
# ----------------------------------------------------------------------

def prepare_order(
    mt5,
    symbol: str,
    signal_dict: dict,
    equity: float,
    daily_pnl_pct: float,
    consecutive_losses: int,
    has_open_position: bool,
):
    allowed, reason = risk_guard(
        current_daily_pnl_pct=daily_pnl_pct,
        consecutive_losses=consecutive_losses,
        has_open_position=has_open_position,
        cfg=CFG,
    )

    if not allowed:
        return None, reason

    if signal_dict.get("Signal", 0) == 0:
        return None, "neutral_signal"

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if info is None or tick is None:
        return None, "symbol_info_unavailable"

    side = signal_dict["Side"]
    market_price = tick.ask if side == "buy" else tick.bid

    # Mantém o stop/take calculados sobre o candle de gatilho.
    # Se o preço atual se afastou demais da entrada teórica, não persegue preço.
    theoretical_entry = float(signal_dict["Entry"])
    atr = float(signal_dict["ATR"])
    max_slippage_from_signal = 0.15 * atr

    if abs(market_price - theoretical_entry) > max_slippage_from_signal:
        return None, "entry_too_far_from_signal"

    stop = float(signal_dict["Suggested_SL"])
    risk_distance = abs(market_price - stop)

    # Recalcula TP em 2R a partir do preço REAL de execução.
    if side == "buy":
        take = market_price + CFG.rr_target * risk_distance
    else:
        take = market_price - CFG.rr_target * risk_distance

    volume = position_size_by_risk(
        equity=equity,
        risk_pct=CFG.risk_per_trade_pct,
        entry=market_price,
        stop=stop,
        tick_size=float(info.trade_tick_size or info.point),
        tick_value=float(info.trade_tick_value),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
    )

    if volume <= 0:
        return None, "minimum_lot_exceeds_risk"

    order = {
        "symbol": symbol,
        "side": side,
        "entry": market_price,
        "stop": stop,
        "take": take,
        "volume": volume,
        "risk_pct": CFG.risk_per_trade_pct,
        "strategy": "pullback_liquidity",
    }

    return order, "ok"


# ----------------------------------------------------------------------
# alerts.py / Telegram
# ----------------------------------------------------------------------

def format_telegram_signal(signal_dict: dict) -> str:
    if signal_dict.get("Signal", 0) == 0:
        return ""

    side = signal_dict["Side"].upper()

    return (
        f"🥇 PULLBACK + LIQUIDEZ | {side}\n"
        f"Entrada: {signal_dict['Entry']:.2f}\n"
        f"SL: {signal_dict['Suggested_SL']:.2f}\n"
        f"TP: {signal_dict['Suggested_TP']:.2f}\n"
        f"R:R: 1:{signal_dict['RR']:.2f}\n"
        f"ATR: {signal_dict['ATR']:.2f}\n"
        f"ADX: {signal_dict['ADX']:.1f}\n"
        f"Liquidez: {signal_dict['Liquidity_Level']:.2f}\n"
        f"Gatilho: tendência + pullback + sweep + reclaim"
    )


# ----------------------------------------------------------------------
# Logging: NÃO grave só candle + Signal.
# Grave o ciclo completo do trade.
# ----------------------------------------------------------------------

TRADE_LOG_COLUMNS = [
    "signal_time",
    "strategy",
    "side",
    "entry",
    "sl",
    "tp",
    "volume",
    "risk_pct",
    "atr",
    "adx",
    "liquidity_level",
    "sweep_extreme",
    "status",       # OPEN / WIN / LOSS / BE / CANCELLED
    "exit_time",
    "exit_price",
    "pnl_cash",
    "result_r",
    "mae_price",
    "mfe_price",
    "spread_entry",
    "slippage_entry",
    "exit_reason",  # TP / SL / TRAILING / MANUAL / DAILY_STOP
]


def result_in_r(side: str, entry: float, stop: float, exit_price: float) -> float:
    initial_risk = abs(entry - stop)
    if initial_risk <= 0:
        return 0.0

    pnl_price = (exit_price - entry) if side == "buy" else (entry - exit_price)
    return pnl_price / initial_risk
