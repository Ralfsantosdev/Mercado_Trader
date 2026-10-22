"""
Alertas automáticos via Telegram.

Como funciona:
- Um bot do Telegram (criado via @BotFather) envia mensagem para o seu chat
  quando um SINAL MUDA (neutro -> compra, neutro -> venda, ou troca de lado).
- O estado do último sinal é salvo em JSON local, então cada mudança só gera
  UMA notificação (sem spam a cada refresh).
- Fora do Streamlit, este módulo roda sozinho: python alerts.py — ideal para
  um cron/agendador que verifica a cada N minutos sem precisar do app aberto.

Configuração (em .streamlit/secrets.toml):
    telegram_token  = "123456:ABC..."
    telegram_chat   = "123456789"

Como criar o bot:
1. No Telegram, fale com @BotFather e use /newbot -> guarde o token.
2. Fale com o seu próprio bot (aperte Start).
3. Descubra seu chat_id: envie uma msg ao bot e depois acesse
   https://api.telegram.org/bot<TOKEN>/getUpdates — o "chat": {"id": ...}.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any

from safety import (
    atomic_write_json,
    load_json,
    trading_allowed,
    warn,
)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_state.json")


def get_credentials() -> tuple:
    """Lê token/chat do secrets.toml do Streamlit ou de variáveis de ambiente.

    Aceita os dois padrões de nome de env var: MAIÚSCULO (TELEGRAM_TOKEN) e o
    minúsculo (telegram_token) — o pessoal costuma colar no deploy os nomes
    exatos do secrets.toml.
    """
    token = os.environ.get("TELEGRAM_TOKEN", "") or os.environ.get("telegram_token", "")
    chat = os.environ.get("TELEGRAM_CHAT", "") or os.environ.get("telegram_chat", "")
    try:
        import streamlit as st
        token = st.secrets.get("telegram_token", token) or token
        chat = st.secrets.get("telegram_chat", chat) or chat
    except Exception:
        pass
    return token, chat


def configured() -> bool:
    token, chat = get_credentials()
    return bool(token and chat)


def send_telegram(text: str, token: Optional[str] = None, chat: Optional[str] = None) -> bool:
    """Envia uma mensagem. Retorna True se o Telegram confirmou."""
    token = token or get_credentials()[0]
    chat = chat or get_credentials()[1]
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=body)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception:
        return False


def send_test_alert() -> str:
    """Mensagem de teste — usada no app para validar a configuração."""
    ok = send_telegram("✅ Alerta de teste do Forex Dashboard.\nSinais do seu bot estão funcionando.")
    if ok:
        return "✅ Teste enviado com sucesso para o seu Telegram."
    return "❌ Falha ao enviar. Confira telegram_token/telegram_chat no secrets.toml."


# Estado do último erro notificado — evita inundar o Telegram repedindo o
# mesmo erro a cada 5 min do cron (mesma ideia do dedup de sinal).
_LAST_ERROR = {"msg": "", "ts": ""}


def notify_error(message: str, cooldown_s: int = 1800) -> bool:
    """
    Envia FALHA operacional ao Telegram (antes tudo ia só pra /tmp/*.log, com o
    stdout do cron indo pra /dev/null — dava pra ficar dias sem operar sem ver
    nada). Deduplica por mensagem durante `cooldown_s`.
    """
    if not configured():
        return False
    if _LAST_ERROR["msg"] == message:
        return False
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(_LAST_ERROR["ts"])).total_seconds()
        if elapsed < cooldown_s:
            return False
    except Exception:
        pass
    ok = send_telegram(f"🚨 *ERRO DO ROBÔ*\n{message}")
    if ok:
        _LAST_ERROR["msg"] = message
        _LAST_ERROR["ts"] = datetime.now().isoformat()
    return ok


def _load_state() -> dict:
    data = load_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        warn(f"falha ao gravar {STATE_FILE}: {e}")


def notify_signal_rich(
    symbol: str,
    signal: int,
    price: float,
    interval: str = "",
    signal_meta: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Optional[str]:
    """
    Envia alerta Telegram RICO com todos os metadados do sinal.

    `signal_meta` deve conter (todos opcionais, exibidos se presentes):
        side, entry, sl, tp, rr, atr, adx, liquidity_level,
        sweep_extreme, trigger_reason

    Quando `signal_meta` é None, cai no formato básico legado.
    Retorna a mensagem enviada ou None se não foi enviado.
    """
    label_map = {1: "🟢 BUY", -1: "🔴 SELL", 0: "⚪ NEUTRO"}
    emoji_map = {1: "🟢", -1: "🔴", 0: "⚪"}

    if signal == 0 and not force:
        return None

    state = _load_state()
    last = state.get(symbol, {}).get("signal", 0)
    if last == signal and not force:
        return None

    ts = datetime.now().strftime("%d/%m %H:%M")
    m = signal_meta or {}

    # ── Linha de cabeçalho — enriquecida se vier com strategy_name ──────────
    strategy = m.get("strategy_name", "PULLBACK + LIQUIDEZ")
    side_label = label_map.get(signal, str(signal))
    header = f"🥇 {strategy} | {side_label}"

    lines = [header, ""]

    # ── Execução ──────────────────────────────────────────────────────────
    entry = m.get("entry") or price
    sl = m.get("sl")
    tp = m.get("tp")
    rr = m.get("rr")

    lines.append(f"Entrada: `{float(entry):.4f}`")
    if sl:
        lines.append(f"SL: `{float(sl):.4f}`")
    if tp:
        lines.append(f"TP: `{float(tp):.4f}`")

    # ── Qualidade ─────────────────────────────────────────────────────────
    if rr or m.get("atr") or m.get("adx"):
        lines.append("")
        if rr:
            lines.append(f"R:R: `1:{float(rr):.2f}`")
        if m.get("atr"):
            lines.append(f"ATR: `{float(m['atr']):.4f}`")
        if m.get("adx"):
            lines.append(f"ADX: `{float(m['adx']):.1f}`")
        if m.get("vol_percentile") is not None:
            _vpm = float(m.get("stop_mult") or 1.0)
            lines.append(
                f"Volatilidade: `{float(m['vol_percentile']):.0f}` "
                f"({m.get('vol_regime') or '—'}) · stop ×`{_vpm:.2f}`"
            )
        if m.get("liquidity_level"):
            lines.append(f"Liquidez: `{float(m['liquidity_level']):.4f}`")
        if m.get("sweep_extreme"):
            lines.append(f"Sweep em: `{float(m['sweep_extreme']):.4f}`")

    # ── Clarezas ─────────────────────────────────────────────────────────
    if m.get("clarezas"):
        lines.append("")
        lines.append(f"📊 Clarezas: `{m['clarezas']}`")

    # ── Gatilho ───────────────────────────────────────────────────────────
    reason = m.get("trigger_reason") or m.get("signal_reason", "")
    if reason:
        lines.append("")
        lines.append("Gatilho:")
        lines.append(f"`{reason}`")

    # ── Rodapé ───────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"Par: `{symbol}` | TF: `{interval}` | {ts}")
    lines.append("_Auditoria automática. Sinal ≠ garantia._")

    msg = "\n".join(lines)

    if not send_telegram(msg):
        return None

    state[symbol] = {"signal": signal, "price": price, "ts": datetime.now().isoformat()}
    _save_state(state)
    return msg


def notify_signal_change(symbol: str, signal: int, price: float, interval: str = "",
                         force: bool = False,
                         signal_meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Notifica SÓ quando o sinal muda. Retorna a mensagem enviada (ou None).

    Quando `signal_meta` está presente, delega para notify_signal_rich().
    Caso contrário, usa o formato legado simples.
    force=True ignora o estado salvo (usado para teste no app).
    """
    if signal_meta:
        return notify_signal_rich(symbol, signal, price, interval, signal_meta, force)

    # ── Formato básico legado ─────────────────────────────────────────
    label = {1: "🟢 COMPRA", -1: "🔴 VENDA", 0: "⚪ Neutro"}[signal]
    if signal == 0 and not force:
        return None

    state = _load_state()
    last = state.get(symbol, {}).get("signal", 0)
    if last == signal and not force:
        return None

    ts = datetime.now().strftime("%d/%m %H:%M")
    msg = (
        f"📈 *Sinal: {label}*\n"
        f"Par: `{symbol}`\n"
        f"Preço: `{price:.5f}`\n"
        f"Timeframe: `{interval}`\n"
        f"Horário: {ts}\n\n"
        f"_Reaja no seu próprio critério. Sinal ≠ lucro._"
    )
    if not send_telegram(msg):
        return None

    state[symbol] = {"signal": signal, "price": price, "ts": datetime.now().isoformat()}
    _save_state(state)
    return msg


def reset_state(symbol: Optional[str] = None) -> None:
    """Zera o estado para re-armar os alertas (após mudar parâmetros, por exemplo)."""
    state = _load_state()
    if symbol:
        state.pop(symbol, None)
    else:
        state = {}
    _save_state(state)


def check_signal(symbol: str = "EUR-USD", interval: str = "15m", days: int = 30,
                 rsi_ob: float = 70, rsi_os: float = 30, use_cross: bool = True,
                 trigger_mode: str = "pullback_liquidity", pullback_ema: int = 50,
                 use_volume: bool = True, session: str = "Londres + Nova York (melhor janela)",
                 min_adx: float = 20.0, force: bool = False,
                 execute: bool = False, sl_atr: float = 1.5, tp_atr: float = 3.0,
                 risk_pct: float = 0.25,
                 pip_size: float = 0.0,
                 fimathe_hour: int = 20, fimathe_candles: int = 4,
                 fimathe_max_pips: float = 30.0,
                  source: str = "mt5",
                  smc_alerts: bool = False,
                  max_open_positions: int = 1,
                  max_vol_percentile: float = 90.0,
                  min_efficiency_ratio: float = 0.15,
                  vol_adaptive_stop: bool = True,
                  tp_pips: float = 0.0,
                  trailing_stop_pips: float = 0.0,
                  dynamic_risk_margin: float = 0.0,
                  max_spread_pips: float = 0.0,
                  min_tick_volume: float = 0.0,
                  tz_offset_hours: Optional[float] = None) -> str:
    """
    Verifica o último candle e notifica o Telegram SE o sinal mudou.
    Com execute=True, também executa a ordem no MetaTrader 5 (abre/fecha
    automaticamente), igual ao dashboard faz em modo manual.
    Uso no cron (sem o app aberto). Retorna um resumo para log.
    """
    from data_provider import get_provider
    from indicators import add_all_indicators
    from model import generate_rule_based_signals, signal_index
    import pandas as pd

    # ── KILL-SWITCH: lido ANTES de qualquer coisa. Se TRADING_DISABLED existir,
    # o robô não opera (nem notifica sinal de entrada como se fosse operar).
    allowed, disabled_reason = trading_allowed()
    if execute and not allowed:
        return f"🛑 Trading desativado ({disabled_reason}) — nada foi executado."

    if execute and not configured():
        return "❌ Telegram não configurado (falta telegram_token/telegram_chat) — não vou operar sem notificação."

    api_key = os.environ.get("AWESOMEAPI_API_KEY", "") or os.environ.get("AWESOMEAPI_KEY", "")
    try:
        import streamlit as st
        api_key = api_key or (st.secrets.get("awesomeapi_key", "") or "")
    except Exception:
        pass
    api_key = api_key.strip()

    # pip_size correto por ativo (XAU/JPY = 0.01, índices = 1.0, majors = 0.0001)
    if pip_size <= 0:
        _indices = ("US500", "NAS100", "US30", "GER40", "IBOV")
        if any(k in symbol.upper() for k in ("XAU", "JPY")):
            pip_size = 0.01
        elif any(k in symbol.upper() for k in _indices):
            pip_size = 1.0
        else:
            pip_size = 0.0001

    # Fuso do servidor: candles do MT5 estão no horário da corretora, não em UTC.
    # Sem subtrair o offset a janela de sessão fica 2–3 h errada.
    if tz_offset_hours is None:
        tz_offset_hours = 0.0
        if source == "mt5":
            try:
                from mt5_bridge import get_bridge
                tz_offset_hours = float(get_bridge().utc_offset_hours() or 0.0)
            except Exception as e:
                warn(f"não consegui detectar o fuso do servidor MT5 ({e}) — usando offset 0")

    # Filtro de confirmação ao vivo (spread/tick) — antes só existia no app.
    _live_quote = None
    if max_spread_pips > 0 or min_tick_volume > 0:
        try:
            from executor import mt5_symbol
            from mt5_bridge import get_bridge
            _live_quote = get_bridge().quote(mt5_symbol(symbol))
            if not _live_quote:
                _live_quote = None
        except Exception as e:
            warn(f"confirmação ao vivo indisponível: {e}")
            _live_quote = None

    try:
        df = get_provider(source=source, api_key=api_key or None).get_ohlcv(
            symbol, days=days, interval=interval
        )
    except Exception as e:
        msg = f"Erro na fonte {source} ({symbol} {interval}): {e}"
        notify_error(msg)
        return f"❌ {msg}"
    if df.empty:
        return f"⚠️ Sem dados para {symbol} {interval}."

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
        pip_size=pip_size,
        fimathe_hour=fimathe_hour,
        fimathe_candles=fimathe_candles,
        fimathe_max_pips=fimathe_max_pips,
        max_vol_percentile=float(max_vol_percentile),
        min_efficiency_ratio=float(min_efficiency_ratio),
        tz_offset_hours=float(tz_offset_hours or 0.0),
        live=_live_quote,
        max_spread_pips=float(max_spread_pips or 0.0),
        min_tick_volume=float(min_tick_volume or 0.0),
        pls_config_kwargs={
            "max_vol_percentile": float(max_vol_percentile),
            "min_efficiency_ratio": float(min_efficiency_ratio),
            "adaptive_stop": bool(vol_adaptive_stop),
        },
    )

    # ── Lê o candle ACIONÁVEL (não necessariamente o último) ──────────────
    # O PLS grava em -2 (candle fechado); ler -1 devolve sempre 0 = robô no-op.
    ai = signal_index(df, trigger_mode)
    last_signal = int(df["Signal"].iloc[ai])
    last_close = float(df["Close"].iloc[ai])
    last_atr = float(df["ATR"].iloc[ai]) if pd.notna(df["ATR"].iloc[ai]) else 0.0
    last_adx = float(df["ADX"].iloc[ai]) if "ADX" in df.columns and pd.notna(df["ADX"].iloc[ai]) else 0.0

    # Stops explícitos da estratégia (ex.: Fimathe) para a execução no MT5
    _sug_sl = 0.0
    _sug_tp = 0.0
    if "Suggested_SL" in df.columns and pd.notna(df["Suggested_SL"].iloc[ai]):
        _sug_sl = float(df["Suggested_SL"].iloc[ai])
    if "Suggested_TP" in df.columns and pd.notna(df["Suggested_TP"].iloc[ai]):
        _sug_tp = float(df["Suggested_TP"].iloc[ai])

    # Regime de volatilidade precisa: multiplicador do stop + diagnóstico
    _vol_mult = 1.0
    _vol_pct = None
    _vol_regime = ""
    if "Vol_Stop_Mult" in df.columns and pd.notna(df["Vol_Stop_Mult"].iloc[ai]):
        _vm = float(df["Vol_Stop_Mult"].iloc[ai])
        if _vm > 0 and vol_adaptive_stop:
            _vol_mult = _vm
    if "Vol_Percentile" in df.columns and pd.notna(df["Vol_Percentile"].iloc[ai]):
        _vol_pct = float(df["Vol_Percentile"].iloc[ai])
    if "Vol_Regime" in df.columns:
        _vol_regime = str(df["Vol_Regime"].iloc[ai] or "")

    # Monta metadados ricos do sinal para o Telegram e journal
    signal_meta: Optional[Dict[str, Any]] = None
    if last_signal != 0:
        signal_meta = {
            "strategy_name": "PULLBACK + LIQUIDEZ" if trigger_mode == "pullback_liquidity" else trigger_mode.upper().replace("_", " "),
            "entry": last_close,
            "sl": _sug_sl or None,
            "tp": _sug_tp or None,
            "rr": df.attrs.get("signal_rr"),
            "atr": last_atr or None,
            "adx": last_adx or None,
            "trigger_reason": df.attrs.get("signal_reason", ""),
            "vol_percentile": _vol_pct,
            "vol_regime": _vol_regime,
            "stop_mult": _vol_mult,
        }
        # Enriquece com dados do módulo PLS quando disponível
        pls_sig = df.attrs.get("_pls_signal")
        if pls_sig is not None:
            signal_meta.update({
                "rr": pls_sig.rr,
                "atr": pls_sig.atr,
                "adx": pls_sig.adx,
                "liquidity_level": pls_sig.liquidity_level,
                "sweep_extreme": pls_sig.sweep_extreme,
                "trigger_reason": pls_sig.reason,
                "sl": pls_sig.stop,
                "tp": pls_sig.take,
            })
        # Enriquece com dados Clarezas quando disponível
        if trigger_mode == "clarezas":
            _last_row = df.iloc[ai]
            _clarezas_tags = []
            if "Clarezas_BuyFlow" in df.columns and _last_row.get("Clarezas_BuyFlow"):
                _clarezas_tags.append("fluxo compra")
            if "Clarezas_SellFlow" in df.columns and _last_row.get("Clarezas_SellFlow"):
                _clarezas_tags.append("fluxo venda")
            if "Clarezas_Absorption" in df.columns and _last_row.get("Clarezas_Absorption"):
                _clarezas_tags.append("absorção")
            if "Clarezas_ContinuationBuy" in df.columns and _last_row.get("Clarezas_ContinuationBuy"):
                _clarezas_tags.append("continuação alta")
            if "Clarezas_ContinuationSell" in df.columns and _last_row.get("Clarezas_ContinuationSell"):
                _clarezas_tags.append("continuação baixa")
            if _clarezas_tags:
                signal_meta["clarezas"] = ", ".join(_clarezas_tags)

    # SMC / Liquidez Alert logic
    fvg_alert = None
    if smc_alerts:
        from indicators import fvg_strength, get_active_fvgs
        # Zonas calculadas SÓ com candles FECHADOS: o candle em formação muda o
        # Low/High a cada tick, o que faria a zona "mudar" a cada refresh e
        # disparar milhares de mensagens no Telegram.
        fvg_df = df.iloc[:-1] if len(df) > 1 else df
        bull_gaps, bear_gaps = get_active_fvgs(fvg_df, max_lookback=100)
        
        # Margem de aproximação de 0.15% (15 pips para 1.0000)
        margin = 0.0015
        
        # Encontra o gap de suporte mais próximo (abaixo do preço)
        closest_bull = None
        for g in bull_gaps:
            if last_close >= g["top"]:
                if closest_bull is None or (last_close - g["top"]) < (last_close - closest_bull["top"]):
                    closest_bull = g
                    
        # Encontra o gap de resistência mais próximo (acima do preço)
        closest_bear = None
        for g in bear_gaps:
            if last_close <= g["bot"]:
                if closest_bear is None or (g["bot"] - last_close) < (closest_bear["bot"] - last_close):
                    closest_bear = g

        if closest_bull and 0 <= (last_close - closest_bull["top"]) / closest_bull["top"] <= margin:
            fvg_alert = (
                f"🧲 *SMC / ZONA DE LIQUIDEZ*\n"
                f"Moeda: `{symbol}`\n"
                f"💰 *PONTO DE COMPRA*\n"
                f"Preço testando o FVG de suporte em `{closest_bull['top']:.5f}`.\n"
                f"💪 Força do nível (liquidez): *{fvg_strength(closest_bull)}* "
                f"({closest_bull['touches']} toque{'s' if closest_bull['touches'] != 1 else ''}).\n"
                f"⚠️ Com 3+ toques a zona perde força e tende a quebrar.\n"
                f"⏳ *Espere a confirmação do preço* (rejeição no suporte) antes de entrar."
            )
            fvg_key_id = f"bull:{closest_bull['top']:.5f}:{closest_bull['touches']}"
        elif closest_bear and 0 <= (closest_bear["bot"] - last_close) / closest_bear["bot"] <= margin:
            fvg_alert = (
                f"🧲 *SMC / ZONA DE LIQUIDEZ*\n"
                f"Moeda: `{symbol}`\n"
                f"💰 *PONTO DE VENDA*\n"
                f"Preço testando o FVG de resistência em `{closest_bear['bot']:.5f}`.\n"
                f"💪 Força do nível (liquidez): *{fvg_strength(closest_bear)}* "
                f"({closest_bear['touches']} toque{'s' if closest_bear['touches'] != 1 else ''}).\n"
                f"⚠️ Com 3+ toques a zona perde força e tende a quebrar.\n"
                f"⏳ *Espere a confirmação do preço* (rejeição na resistência) antes de entrar."
            )
            fvg_key_id = f"bear:{closest_bear['bot']:.5f}:{closest_bear['touches']}"

    if fvg_alert:
        state = _load_state()
        fvg_key = f"{symbol}_fvg"
        last_fvg = state.get(fvg_key, {}).get("key", "")
        last_ts = state.get(fvg_key, {}).get("ts", "")
        # Cooldown de 5 min por símbolo: evita rajada quando o preço oscila
        # na fronteira da zona e a chave muda (nível vizinho, toque a mais).
        cooldown_ok = True
        if last_ts and not force:
            try:
                cooldown_ok = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() >= 300
            except Exception:
                cooldown_ok = True
        if (last_fvg != fvg_key_id and cooldown_ok) or force:
            # Só grava o estado SE o Telegram confirmou o envio — antes o
            # estado era salvo mesmo em falha e o alerta se perdia pra sempre.
            if send_telegram(fvg_alert):
                state[fvg_key] = {"key": fvg_key_id, "msg": fvg_alert, "ts": datetime.now().isoformat()}
                _save_state(state)

    # notifica o Telegram se o sinal mudou (com metadados ricos quando disponível)
    msg = notify_signal_change(symbol, last_signal, last_close, interval,
                               force=force, signal_meta=signal_meta)

    # Registra no journal se o sinal foi notificado (para auditoria posterior)
    if msg and last_signal != 0 and signal_meta:
        try:
            from journal import open_trade
            open_trade(
                symbol=symbol,
                side=last_signal,
                entry=signal_meta.get("entry") or last_close,
                sl=signal_meta.get("sl") or 0.0,
                tp=signal_meta.get("tp") or 0.0,
                volume=0.0,  # preenchido depois pelo executor
                risk_pct=risk_pct,
                atr=signal_meta.get("atr") or 0.0,
                adx=signal_meta.get("adx") or 0.0,
                liquidity_level=signal_meta.get("liquidity_level") or 0.0,
                sweep_extreme=signal_meta.get("sweep_extreme") or 0.0,
                trigger_reason=signal_meta.get("trigger_reason", ""),
                signal_time=datetime.now().isoformat(),
            )
        except Exception as e:
            warn(f"journal open_trade: {e}")  # auditoria não bloqueia, mas deixa rastro

    # ── executa no MT5 ────────────────────────────────────────────────────
    # Sob LOCK: o cron de alertas e o cron de trader disparam no MESMO minuto e
    # o app roda de 10 em 10 s — sem mutex dois processos liam "sem posição"
    # ao mesmo tempo e abriam a mesma ordem duas vezes.
    exec_msg = ""
    if execute:
        from safety import execution_lock
        with execution_lock(blocking=True) as got_lock:
            if not got_lock:
                exec_msg = "⏳ Outro executor ativo — tick ignorado (sem ordem duplicada)."
            else:
                try:
                    from executor import Mt5Broker
                    broker = Mt5Broker()
                    if broker.available():
                        exec_msg = broker.on_signal(
                            symbol, last_signal, last_close, last_atr,
                            sl_atr=sl_atr, tp_atr=tp_atr, risk_pct=risk_pct,
                            tp_pips=tp_pips,
                            trailing_stop_pips=trailing_stop_pips,
                            dynamic_risk_margin=dynamic_risk_margin,
                            suggested_sl=_sug_sl, suggested_tp=_sug_tp,
                            pip_size=pip_size,
                            max_open_positions=max_open_positions,
                            vol_mult=_vol_mult,
                        )
                        _sync_journal(exec_msg, getattr(broker, "last_order", None))
                    else:
                        exec_msg = "❌ MT5 indisponível (terminal/bridge não rodando)."
                        notify_error(f"MT5 indisponível — {symbol} {interval}. "
                                     "Terminal/bridge fora do ar, nenhuma ordem foi enviada.")
                except Exception as e:
                    exec_msg = f"❌ Erro ao executar no MT5: {e}"
                    notify_error(f"Exceção ao executar {symbol}: {e}")

    # Resultado da ORDEM vai ao Telegram — antes ia só pro /tmp/trader.log.
    if execute and exec_msg and any(k in exec_msg for k in ("ABRIU", "FECHOU", "❌", "🛑", "🛡️")):
        if not any(k in exec_msg for k in ("sem posição", "sinal 0")):
            send_telegram(f"📡 *EXECUÇÃO* `{symbol}` {interval}\n{exec_msg}")

    label = {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal]
    parts = [f"{symbol} {interval}: {label}"]
    _vol_block = df.attrs.get("vol_block_msg", "") or ""
    if _vol_block:
        parts.append(f"🌋 bloqueado: {_vol_block}")
    _live_block = df.attrs.get("signal_block_msg", "") or ""
    if _live_block:
        parts.append(f"⛔ ao vivo: {_live_block}")
    if msg:
        parts.append("Telegram notificado")
    if execute:
        parts.append(exec_msg if exec_msg else "sem posição/sinal")
    return " | ".join(parts)


def _sync_journal(exec_msg: str, order: Optional[Dict[str, Any]]) -> None:
    """
    Liga o journal ao executor: preenche o volume real e fecha a entrada.

    `journal.close_trade()`/`update_mae_mfe()` existiam mas NUNCA eram chamados —
    o diário de consistência ficava para sempre com `{"entries": []}` e sumia do
    resumo (que só conta entradas com status != open).
    """
    if not exec_msg or not order:
        return
    try:
        import journal
        action = order.get("action")
        if action == "open":
            state = journal._load_state()
            entries = state.get("entries", [])
            for e in reversed(entries):
                if e.get("symbol") == order.get("symbol") and e.get("status") == "open":
                    e["volume"] = order.get("size", 0.0)
                    if order.get("price"):
                        e["entry"] = order["price"]
                    if order.get("sl"):
                        e["sl"] = order["sl"]
                    if order.get("tp"):
                        e["tp"] = order["tp"]
                    if order.get("ticket"):
                        e["ticket"] = order["ticket"]
                    break
            journal._save_state(state)
        elif action == "close":
            state = journal._load_state()
            entries = state.get("entries", [])
            target = None
            for i in range(len(entries) - 1, -1, -1):
                if entries[i].get("symbol") == order.get("symbol") and entries[i].get("status") == "open":
                    target = i
                    break
            if target is not None:
                journal.close_trade(
                    idx=target,
                    exit_price=float(order.get("price") or 0.0),
                    exit_reason=str(order.get("reason") or "signal"),
                    pnl_cash=float(order.get("pnl") or 0.0),
                )
    except Exception as e:
        warn(f"journal sync: {e}")


def _cli_kwargs(args) -> dict:
    """Traduz argparse -> kwargs de check_signal (fonte única: os dois caminhos
    do CLI — loop contínuo e uma-execução — deixaram de divergir)."""
    return dict(
        symbol=args.symbol, interval=args.interval, days=args.days,
        rsi_ob=args.rsi_ob, rsi_os=args.rsi_os,
        use_cross=not args.no_cross, use_volume=not args.no_volume,
        trigger_mode=args.trigger_mode, pullback_ema=args.pullback_ema,
        session=args.session, min_adx=args.min_adx, force=args.force,
        execute=args.execute, sl_atr=args.sl_atr, tp_atr=args.tp_atr,
        risk_pct=args.risk_pct, pip_size=args.pip_size,
        fimathe_hour=args.fimathe_hour, fimathe_candles=args.fimathe_candles,
        fimathe_max_pips=args.fimathe_max_pips, source=args.source,
        smc_alerts=args.smc_alerts,
        max_vol_percentile=args.vol_max_pct,
        min_efficiency_ratio=args.vol_er_min,
        vol_adaptive_stop=not args.no_vol_adaptive,
        max_open_positions=args.max_open,
        tp_pips=args.tp_pips,
        trailing_stop_pips=args.trailing_stop_pips,
        dynamic_risk_margin=args.dynamic_risk_margin,
        max_spread_pips=args.max_spread_pips,
        min_tick_volume=args.min_tick_volume,
        tz_offset_hours=args.tz_offset,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Alertas Telegram + execução MT5 (standalone p/ cron).")
    parser.add_argument("--test", action="store_true", help="Envia mensagem de teste e sai")
    parser.add_argument("--check", action="store_true", help="Verifica sinal e notifica se mudou")
    parser.add_argument("--execute", action="store_true",
                        help="Também executa a ordem no MetaTrader 5 (abre/fecha automático)")
    parser.add_argument("--symbol", default="EUR-USD", help="Par (default: EUR-USD)")
    parser.add_argument("--interval", default="15m", choices=["1m", "2m", "5m", "15m", "30m", "1h", "1d"])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--rsi-ob", type=float, default=70)
    parser.add_argument("--rsi-os", type=float, default=30)
    parser.add_argument("--no-cross", action="store_true", help="Usa nível MM9/MM21 em vez de cruzamento")
    parser.add_argument("--trigger-mode", default="pullback_liquidity",
                        choices=["auto", "slope", "cross", "pullback", "pullback_liquidity", "clarezas", "3_9_20", "fimathe_tokio"],
                        help="Gatilho do sinal. pullback_liquidity (padrão SMC), clarezas (fluxo+absorção+continuação).")
    parser.add_argument("--pullback-ema", type=int, default=50, help="EMA usada no gatilho pullback")
    parser.add_argument("--no-volume", action="store_true")
    parser.add_argument("--session", default="Londres + Nova York (melhor janela)")
    parser.add_argument("--min-adx", type=float, default=20.0)
    parser.add_argument("--vol-max-pct", type=float, default=90.0,
                        help="Bloqueia sinal acima desse percentil de volatilidade (100 = desligado)")
    parser.add_argument("--vol-er-min", type=float, default=0.15,
                        help="Bloqueia sinal com Efficiency Ratio (chop) abaixo disso (0 = desligado)")
    parser.add_argument("--no-vol-adaptive", action="store_true",
                        help="Desliga o stop adaptativo à volatilidade (Vol_Stop_Mult)")
    parser.add_argument("--sl-atr", type=float, default=1.5)
    parser.add_argument("--tp-atr", type=float, default=3.0)
    parser.add_argument("--risk-pct", type=float, default=0.25)
    parser.add_argument("--pip-size", type=float, default=0.0,
                        help="Valor de 1 pip (0 = auto por ativo: 0.01 p/ XAU-JPY, "
                             "1.0 p/ índices, 0.0001 p/ majors)")
    parser.add_argument("--fimathe-hour", type=int, default=20, help="Abertura Fimathe (0-23 UTC)")
    parser.add_argument("--fimathe-candles", type=int, default=4, help="Duração do Canal de Referência")
    parser.add_argument("--fimathe-max-pips", type=float, default=30.0, help="Fatiamento do canal em pips")
    parser.add_argument("--force", action="store_true", help="Ignora o estado salvo e notifica sempre")
    parser.add_argument("--max-open", type=int, default=1,
                        help="Máximo de posições simultâneas no portfólio (1 por par operado)")
    parser.add_argument("--tp-pips", type=float, default=0.0,
                        help="Take profit fixo em pips (0 = usar múltiplo de ATR)")
    parser.add_argument("--trailing-stop-pips", type=float, default=0.0,
                        help="Trailing stop em pips no MT5 (0 = desligado)")
    parser.add_argument("--dynamic-risk-margin", type=float, default=0.0,
                        help="Risco dinâmico pela margem do ML (0 = desligado)")
    parser.add_argument("--max-spread-pips", type=float, default=0.0,
                        help="Bloqueia entrada se o spread ao vivo > N pips (0 = desligado)")
    parser.add_argument("--min-tick-volume", type=float, default=0.0,
                        help="Bloqueia entrada se o volume de tick do dia < N (0 = desligado)")
    parser.add_argument("--tz-offset", type=float, default=None,
                        help="Offset (servidor − UTC) em horas. Omitir = detectar do MT5.")
    parser.add_argument("--source", default="mt5", choices=["awesomeapi", "yfinance", "mt5"], 
                        help="Fonte de dados (padrão: mt5 — execução institucional)")
    parser.add_argument("--continuous", action="store_true", help="Roda continuamente em loop (Event-Driven)")
    parser.add_argument("--smc-alerts", action="store_true", help="Ativa alertas de aproximação de zonas de liquidez SMC (FVG)")
    args = parser.parse_args()

    if args.test or not args.check:
        print(send_test_alert())
    elif args.continuous:
        import time
        print("🚀 Iniciando robô em modo institucional (Event-Driven)...")
        print(f"Fonte de dados: {args.source.upper()}")
        print("Pressione Ctrl+C para interromper.")
        while True:
            try:
                res = check_signal(**_cli_kwargs(args))
                if "Neutro" not in res and "sem dados" not in res and "Erro" not in res:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {res}")
            except KeyboardInterrupt:
                print("\nRobô interrompido pelo usuário.")
                break
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Erro no loop: {e}")
                notify_error(f"Loop contínuo: {e}")
            time.sleep(1)
    elif args.check:
        print(check_signal(**_cli_kwargs(args)))
