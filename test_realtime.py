"""
Testes de regressão das correções para operação AUTOMÁTICA em conta real.

Foco: os bugs que faziam o robô ser um no-op ou operar sem barreira.

    python3 test_realtime.py
"""

import os
import sys
import tempfile
import types

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from executor import LIVE_TRADING_ENV as LIVE_TRADING_ENV_ORIG  # noqa: E402

FAILED = []


def check(name, fn):
    try:
        fn()
        print(f"  OK  {name}")
    except Exception as e:
        FAILED.append(name)
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────── cenario sintético
def make_df(n=400, freq="15min"):
    np.random.seed(7)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    close = 2000 * np.exp(np.cumsum(np.random.normal(0, 0.001, n)))
    high = close * (1 + np.abs(np.random.normal(0, 0.0008, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.0008, n)))
    open_ = close * (1 + np.random.normal(0, 0.0004, n))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": np.random.randint(50, 500, n)},
        index=idx,
    )


class FakePLS:
    def __init__(self, side="buy"):
        self.side = side
        self.stop = 1990.0
        self.take = 2020.0
        self.rr = 3.0
        self.atr = 5.0
        self.adx = 30.0
        self.liquidity_level = 1985.0
        self.sweep_extreme = 1984.0
        self.reason = "pullback na liquidez"


def _with_stubbed_pls(fn, side="buy"):
    import model
    original = model._pls_build_signal
    model._pls_build_signal = lambda *a, **k: FakePLS(side)
    try:
        return fn()
    finally:
        model._pls_build_signal = original


# ───────────────────────────────────────────────────── 1. índice do sinal
def test_signal_index_is_minus2():
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    def run():
        df = add_all_indicators(make_df())
        df = generate_rule_based_signals(df, trigger_mode="pullback_liquidity")
        assert signal_index(df, "pullback_liquidity") == -2, \
            f"attrs={df.attrs.get('signal_idx')} — o PLS grava no candle FECHADO (-2)"
        assert int(df["Signal"].iloc[-2]) != 0, "sinal PLS deveria estar no candle -2"

    _with_stubbed_pls(run)


def test_alerts_path_reads_actionable_candle():
    """Simula exatamente o que alerts.py faz: ler Signal no índice acionável."""
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    def run():
        df = add_all_indicators(make_df())
        df = generate_rule_based_signals(df, trigger_mode="pullback_liquidity")
        ai = signal_index(df, "pullback_liquidity")
        read = int(df["Signal"].iloc[ai])
        stale = int(df["Signal"].iloc[-1])  # o que o robô lia ANTES da correção
        assert read != 0, "caminho corrigido não enxergou o sinal"
        assert stale == 0, "o candle em formação NÃO deveria ter sinal do PLS"
        # stops explícitos também vêm do mesmo candle
        assert pd.notna(df["Suggested_SL"].iloc[ai])
        assert pd.notna(df["Suggested_TP"].iloc[ai])

    _with_stubbed_pls(run)


def test_non_pls_still_uses_last_candle():
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    df = add_all_indicators(make_df())
    df = generate_rule_based_signals(df, trigger_mode="cross")
    assert signal_index(df, "cross") == -1


# ────────────────────────────────────────── 2. filtros agora valem p/ o PLS
def test_session_filter_blocks_pls_outside_window():
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    def run():
        base = make_df()
        # janela 12–16 UTC; força o candle -2 a cair às 03:00
        shifted = base.copy()
        shifted.index = (shifted.index.normalize()
                         + pd.timedelta_range(start=pd.Timedelta(hours=3),
                                              periods=len(shifted), freq="15min"))
        df = add_all_indicators(shifted)
        df = generate_rule_based_signals(
            df, trigger_mode="pullback_liquidity",
            session="Londres + Nova York (melhor janela)")
        ai = signal_index(df, "pullback_liquidity")
        assert int(df["Signal"].iloc[ai]) == 0, \
            "sinal PLS passou por fora da janela de sessão configurada"

    _with_stubbed_pls(run)


def test_session_filter_honours_tz_offset():
    """Servidor UTC+3: candle 14:00 SERVIDOR = 11:00 UTC → fora de 12–16."""
    from model import _is_in_session

    idx = pd.DatetimeIndex(["2026-01-05 14:00"], tz="UTC")  # relógio do servidor
    sess = "Londres + Nova York (melhor janela)"
    assert bool(_is_in_session(idx, sess, tz_offset_hours=0.0).iloc[0]) is True
    assert bool(_is_in_session(idx, sess, tz_offset_hours=3.0).iloc[0]) is False
    # 15:00 servidor = 12:00 UTC → dentro
    idx2 = pd.DatetimeIndex(["2026-01-05 15:00"], tz="UTC")
    assert bool(_is_in_session(idx2, sess, tz_offset_hours=3.0).iloc[0]) is True


def test_adx_filter_blocks_pls():
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    def run():
        df = add_all_indicators(make_df())
        # min_adx absurdamente alto = tendência nunca "forte" o bastante
        df = generate_rule_based_signals(df, trigger_mode="pullback_liquidity",
                                         min_adx=99)
        ai = signal_index(df, "pullback_liquidity")
        assert int(df["Signal"].iloc[ai]) == 0, "ADX filtro ignorado no PLS"

    _with_stubbed_pls(run)


# ─────────────────────────────────────────────── 3. gate demo/real (conta real)
def test_account_gate_blocks_real_without_env():
    from executor import account_gate, live_trading_enabled, LIVE_TRADING_ENV

    os.environ.pop(LIVE_TRADING_ENV, None)
    assert live_trading_enabled() is False
    ok, why = account_gate("real")
    assert ok is False and "ALLOW_LIVE_TRADING" in why, \
        "conta REAL foi liberada sem ALLOW_LIVE_TRADING"

    ok, why = account_gate("demo")
    assert ok is True, "conta demo deveria ser liberada"

    ok, why = account_gate("unknown")
    assert ok is False, "modo desconhecido deveria ser BLOQUEADO"

    os.environ[LIVE_TRADING_ENV] = "1"
    try:
        assert live_trading_enabled() is True
        ok, why = account_gate("real")
        assert ok is True, "ALLOW_LIVE_TRADING=1 deveria liberar conta real"
    finally:
        del os.environ[LIVE_TRADING_ENV]


# ───────────────────────────────────────────────── 4. sizing não força entrada
def test_size_lots_refuses_when_it_should_not_trade():
    from executor import Mt5Broker

    b = Mt5Broker(bridge=object())
    assert b._size_lots(0.0, 0.25, 1.0, price=1.1, symbol="EUR-USD") == 0.0, \
        "equity 0 não pode virar ordem de 0.01 lote"
    assert b._size_lots(10000.0, 0.25, 0.0, price=1.1, symbol="EUR-USD") == 0.0, \
        "SL de distância 0 não pode virar ordem"

    # risco tão pequeno que nem o lote mínimo cabe -> deve recusar, não forçar 0.01
    lots = b._size_lots(1.0, 0.001, 500.0, price=2000.0, symbol="XAU-USD")
    assert lots == 0.0, f"esperava recusa (0.0), veio {lots}"


# ─────────────────────────────────────────────────────── 5. kill-switch
def test_kill_switch_blocks_everything():
    from safety import disable_trading, enable_trading, trading_allowed, kill_switch_reason

    enable_trading()
    allowed, why = trading_allowed()
    assert allowed and why == ""
    try:
        disable_trading("teste de regressão")
        allowed, why = trading_allowed()
        assert allowed is False
        assert "teste de regressão" in why
        # e o alerts.py bloqueia ANTES de calcular sinal
        sys.path.insert(0, BASE)
        from alerts import check_signal
        out = check_signal(execute=True, source="mt5")
        assert out.startswith("🛑"), f"check_signal não respeitou o kill-switch: {out}"
    finally:
        enable_trading()
    assert kill_switch_reason() == ""


# ────────────────────────────────────── 6. risk_guard não libera em erro
def test_daily_risk_fails_safe_when_history_unreadable():
    from executor import Mt5Broker

    class BridgeStub:
        def ensure_ready(self):
            return True, ""
        def account_mode(self):
            return "demo"
        def quote(self, s):
            return {"symbol": s, "bid": 1.0, "ask": 1.0, "point": 0.0001, "digits": 5}
        def account_info(self):
            return {"equity": 10000.0, "balance": 10000.0, "margin_free": 9000.0}
        def positions(self, magic=None):
            return []
        def history_deals_today(self, magic=None):
            return None  # consulta FALHOU (não "sem deals")

    b = Mt5Broker(bridge=BridgeStub())
    daily, consec, blocked = b._daily_risk(b._mt5(), 10000.0)
    assert blocked, "histórico ilegível deveria BLOQUEAR a entrada (fail-safe)"

    b2 = Mt5Broker(bridge=types.SimpleNamespace(**{
        "ensure_ready": lambda: (True, ""),
        "account_mode": lambda: "real",
        "quote": lambda s: {"symbol": s, "bid": 2000.0, "ask": 2000.5,
                            "point": 0.01, "digits": 2},
        "symbol_info": lambda s: {"trade_stops_level": 0},
        "positions": lambda *a, **k: [],
        "account_info": lambda: {"equity": 10000.0, "balance": 10000.0,
                                 "margin_free": 9000.0},
        "history_deals_today": lambda magic=None: None,
    }))
    out = b2.on_signal("XAU-USD", 1, 2000.0, 5.0)
    assert out.startswith("🛑"), f"conta real sem gate passou: {out}"
    assert "ALLOW_LIVE_TRADING" in out, out


# ───────────────────────────────────────────────── 7. JSON atômico / saneamento
def test_state_files_survive_and_do_not_reset():
    from safety import atomic_write_json, load_json
    from executor import PaperBroker

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "execution_state.json")
        capital = {"capital": 12345.67, "initial_capital": 10000.0,
                   "positions": {}, "history": [{"x": 1}], "equity_curve": []}
        atomic_write_json(path, capital)
        p = PaperBroker(state_file=path, initial_capital=999.0)
        assert p.state["capital"] == 12345.67, \
            "capital foi reinicializado num arquivo perfeitamente válido"
        assert len(p.state["history"]) == 1, "histórico apagado"
        p._save()
        assert load_json(path, {})["capital"] == 12345.67


# ─────────────────────────────────────────────────────── 8. magic do robô
def test_bridge_positions_filter_by_magic():
    from mt5_bridge import MT5Bridge, ROBOT_MAGIC

    class P(types.SimpleNamespace):
        pass

    robô = P(ticket=1, symbol="XAUUSD", type=0, volume=0.01, price_open=2000.0,
              sl=1990.0, tp=2020.0, profit=1.0, price_current=2005.0,
              magic=ROBOT_MAGIC, comment="forex_dashboard")
    manual = P(ticket=2, symbol="XAUUSD", type=0, volume=1.0, price_open=1900.0,
               sl=0.0, tp=0.0, profit=99.0, price_current=2010.0,
               magic=0, comment="manual")

    b = MT5Bridge.__new__(MT5Bridge)
    b._conn = None
    b.last_error = ""
    b.host, b.port = "127.0.0.1", 18812
    b._eval = lambda code: (robô, manual) if "positions_get" in code else None

    only_robot = b.positions()
    assert len(only_robot) == 1 and only_robot[0]["ticket"] == 1, \
        "positions() deveria devolver SÓ as do robô"
    assert len(b.positions(magic=None)) == 2, "magic=None deveria devolver todas"


# ────────────────────────────────────────────────── 9. sinal: jornada completa
def test_full_signal_path_reports_actionable():
    """index correto + vol gate + reason montados no candle certo."""
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index

    def run():
        df = add_all_indicators(make_df())
        df = generate_rule_based_signals(
            df, trigger_mode="pullback_liquidity",
            max_vol_percentile=100.0, min_efficiency_ratio=0.0)
        ai = signal_index(df, "pullback_liquidity")
        assert ai == -2
        assert df.attrs.get("signal_reason"), "reason não foi montado no candle acionável"

    _with_stubbed_pls(run)


# ─────────────────────────────────────────── 10. volume profile renderiza
def test_volume_profile_survives_the_chart_layout():
    """Regressão: update_xaxes(range=...) aplicava-se a TODOS os eixos X e
    sobrescrevia o range NUMÉRICO do xaxis2 do perfil com datas -> perfil
    nunca aparecia (o caso que o usuário relatou)."""
    import plotly.graph_objects as go
    from indicators import add_all_indicators, build_volume_profile_plotly
    from model import generate_rule_based_signals

    df = add_all_indicators(make_df())
    df = generate_rule_based_signals(df, trigger_mode="pullback_liquidity")
    df_plot = df.iloc[-120:]

    vp = build_volume_profile_plotly(df_plot,
                                     price_ref=float(df_plot["Close"].iloc[-1]))
    assert vp, "build_volume_profile_plotly devolveu None com volume válido"
    vp_y, vp_x, vp_colors = vp

    future_date = df_plot.index[-1] + pd.Timedelta(minutes=15)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df_plot.index, y=df_plot["Volume"], yaxis="y2",
                         name="Volume (Ticks)"))
    fig.add_trace(go.Bar(x=vp_x, y=vp_y, orientation="h", xaxis="x2",
                         marker=dict(color=vp_colors), name="Vol. por preço"))
    fig.update_layout(
        xaxis=dict(fixedrange=False, domain=[0.0, 0.92]),
        yaxis=dict(fixedrange=False, domain=[0.25, 1.0]),
        xaxis2=dict(anchor="y", side="right", domain=[0.92, 1.0],
                    showticklabels=False, showgrid=False, zeroline=False,
                    range=[0, max(vp_x) * 1.05]),
    )
    fig.update_layout(xaxis=dict(range=[df_plot.index[0], future_date],
                                 rangeslider=dict(thickness=0.08)))

    r2 = fig.layout.xaxis2.range
    assert isinstance(r2[0], (int, float)), \
        f"range do perfil virou {type(r2[0]).__name__} — perfil invisível"
    assert r2[1] > max(vp_x), "range do perfil menor que os dados"
    assert isinstance(fig.layout.xaxis.range[0], pd.Timestamp), \
        "eixo principal perdeu o range de datas"
    bars = [t for t in fig.data if t.name == "Vol. por preço"][0]
    assert len(bars.y) == len(vp_y) > 0




# ─────────────────────────────── 11. modo da conta (BURACO DE SEGURANÇA REAL)
def _bridge_with_account(trade_mode, server):
    import mt5_bridge
    b = mt5_bridge.MT5Bridge.__new__(mt5_bridge.MT5Bridge)
    b.host, b.port = "127.0.0.1", 18812
    b._conn = None
    b.last_error = ""

    class AI:
        pass
    ai = AI()
    ai.trade_mode = trade_mode
    ai.server = server
    ai.equity = 100.0
    b._eval = lambda code: ai if "account_info" in code else None
    return b


def test_account_mode_reads_trade_mode_correctly():
    """ACCOUNT_TRADE_MODE: 0=demo, 1=contest, 2=REAL (ordem da enum MQL5).
    O mapa antigo estava trocado e classificava a conta Live01 como 'contest'
    — e o gate de 'contest' LIBERA operação, ou seja: dinheiro real sem
    ALLOW_LIVE_TRADING."""
    from executor import account_gate

    assert _bridge_with_account(2, "PepperstoneBS-MT5-Live01").account_mode() == "real"
    assert _bridge_with_account(0, "Pepperstone-Demo").account_mode() == "demo"
    assert _bridge_with_account(1, "Pepperstone-Contest").account_mode() == "contest"
    # enum desconhecido
    assert _bridge_with_account(99, "QualquerCoisa").account_mode() == "unknown"

    # fail-safe: servidor dizendo live ganha de qualquer enum
    assert _bridge_with_account(0, "Broker-Live01").account_mode() == "real", \
        "servidor 'live' com enum demo deveria virar real (fail-safe)"

    os.environ.pop(LIVE_TRADING_ENV_ORIG, None)
    ok, why = account_gate(_bridge_with_account(2, "PepperstoneBS-MT5-Live01").account_mode())
    assert ok is False, "conta Live01 passou pelo gate sem ALLOW_LIVE_TRADING"
    assert "ALLOW_LIVE_TRADING" in why


# ─────────────────── 12. pip auto-detect (cron nunca trocava o pip do ouro)
def test_pip_autodetect_is_reachable_from_check_signal():
    """O default era 0.0001 > 0, então o `if pip_size <= 0` do auto-detect
    NUNCA rodava → ouro ficava com pip de forex e o spread real de 0.23 virava
    2300p > 25p: toda entrada bloqueada."""
    import inspect
    from alerts import check_signal
    from model import apply_live_confirmation

    sig = inspect.signature(check_signal)
    assert float(sig.parameters["pip_size"].default) == 0.0, \
        f"default de pip_size = {sig.parameters['pip_size'].default} (precisa ser 0 = auto)"

    df = make_df()
    df["Signal"] = 1
    df.attrs["signal_idx"] = -1
    live = {"bid": 4137.31, "ask": 4137.54, "spread": 0.23, "digits": 2,
            "point": 0.01, "time": int(pd.Timestamp.now(tz="UTC").timestamp()) + 3 * 3600,
            "tick_volume": 0.0, "day_volume": 0.0}

    ok = apply_live_confirmation(df, quote=live, max_spread_pips=60.0,
                                 min_tick_volume=1.0, pip_size=0.01,
                                 tz_offset_hours=3.0)
    assert int(ok["Signal"].iloc[-1]) == 1, \
        f"spread real 0.23 bloqueado: {ok.attrs.get('signal_block_msg')}"

    bad = apply_live_confirmation(df, quote=live, max_spread_pips=60.0,
                                  min_tick_volume=1.0, pip_size=0.0001,
                                  tz_offset_hours=3.0)
    assert int(bad["Signal"].iloc[-1]) == 0, \
        "com pip de forex o spread 0.23 vira 2300p e bloqueia (é o bug antigo)"


# ─────────────── 13. volume "não reportado" ≠ mercado fechado; tick velho sim
def test_live_confirmation_uses_tick_freshness_not_day_volume():
    import time as _t
    from model import apply_live_confirmation
    df = make_df()
    df["Signal"] = 1
    df.attrs["signal_idx"] = -1
    now = int(_t.time())
    fresh = {"bid": 4137.31, "ask": 4137.54, "spread": 0.23, "digits": 2,
             "time": now + 3 * 3600, "tick_volume": 0.0, "day_volume": 0.0}
    stale = dict(fresh, time=now + 3 * 3600 - 7 * 24 * 3600)

    r = apply_live_confirmation(df, quote=fresh, max_spread_pips=60.0,
                                min_tick_volume=1.0, pip_size=0.01,
                                tz_offset_hours=3.0)
    assert int(r["Signal"].iloc[-1]) == 1, \
        f"tick fresco bloqueado: {r.attrs.get('signal_block_msg')}"

    r2 = apply_live_confirmation(df, quote=stale, max_spread_pips=60.0,
                                 min_tick_volume=1.0, pip_size=0.01,
                                 tz_offset_hours=3.0)
    assert int(r2["Signal"].iloc[-1]) == 0, "tick de 7 dias atrás deveria bloquear"
    assert "fechado" in (r2.attrs.get("signal_block_msg") or "")


if __name__ == "__main__":
    print("\n== Regressões das correções para conta real ==")
    check("índice do sinal = candle FECHADO (-2) no PLS", test_signal_index_is_minus2)
    check("caminho do alerts.py enxerga o sinal", test_alerts_path_reads_actionable_candle)
    check("gatilhos não-PLS continuam no candle -1", test_non_pls_still_uses_last_candle)
    check("sessão filtra o sinal PLS", test_session_filter_blocks_pls_outside_window)
    check("offset de fuso do servidor respeitado", test_session_filter_honours_tz_offset)
    check("ADX filtra o sinal PLS", test_adx_filter_blocks_pls)
    check("gate bloqueia conta REAL sem ALLOW_LIVE_TRADING", test_account_gate_blocks_real_without_env)
    check("sizing recusa em vez de forçar 0.01 lote", test_size_lots_refuses_when_it_should_not_trade)
    check("kill-switch bloqueia o robô", test_kill_switch_blocks_everything)
    check("risk_guard falha para o lado SEGURO", test_daily_risk_fails_safe_when_history_unreadable)
    check("JSON de estado não reinicia capital/histórico", test_state_files_survive_and_do_not_reset)
    check("posições filtradas por magic do robô", test_bridge_positions_filter_by_magic)
    check("jornada completa do sinal", test_full_signal_path_reports_actionable)
    check("volume profile sobrevive ao layout do gráfico", test_volume_profile_survives_the_chart_layout)
    check("modo da conta lê ACCOUNT_TRADE_MODE certo (2=REAL)", test_account_mode_reads_trade_mode_correctly)
    check("auto-detect de pip alcançável (cron entende ouro)", test_pip_autodetect_is_reachable_from_check_signal)
    check("tick fresco > volume reportado (fechado de verdade)", test_live_confirmation_uses_tick_freshness_not_day_volume)

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} falha(s): {', '.join(FAILED)}")
        sys.exit(1)
    print("✅ todos os testes passaram")
