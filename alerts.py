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
from typing import Optional

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


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def notify_signal_change(symbol: str, signal: int, price: float, interval: str = "",
                         force: bool = False) -> Optional[str]:
    """
    Notifica SÓ quando o sinal muda. Retorna a mensagem enviada (ou None).

    force=True ignora o estado salvo (usado para teste no app).
    """
    label = {1: "🟢 COMPRA", -1: "🔴 VENDA", 0: "⚪ Neutro"}[signal]
    if signal == 0 and not force:
        return None  # só alerta quando há ordem, não quando o sinal some

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
                 trigger_mode: str = "pullback", pullback_ema: int = 50,
                 use_volume: bool = True, session: str = "Londres + Nova York (melhor janela)",
                 min_adx: float = 20.0, force: bool = False,
                 execute: bool = False, sl_atr: float = 1.5, tp_atr: float = 3.0,
                 risk_pct: float = 0.25,
                 pip_size: float = 0.0001,
                 fimathe_hour: int = 20, fimathe_candles: int = 4,
                 fimathe_max_pips: float = 30.0,
                 source: str = "awesomeapi",
                 smc_alerts: bool = False) -> str:
    """
    Verifica o último candle e notifica o Telegram SE o sinal mudou.
    Com execute=True, também executa a ordem no MetaTrader 5 (abre/fecha
    automaticamente), igual ao dashboard faz em modo manual.
    Uso no cron (sem o app aberto). Retorna um resumo para log.
    """
    from data_provider import get_provider
    from indicators import add_all_indicators
    from model import generate_rule_based_signals
    import pandas as pd

    if execute and not configured():
        return "❌ Telegram não configurado (falta telegram_token/telegram_chat) — não vou operar sem notificação."

    api_key = os.environ.get("AWESOMEAPI_API_KEY", "") or os.environ.get("AWESOMEAPI_KEY", "")
    try:
        import streamlit as st
        api_key = api_key or (st.secrets.get("awesomeapi_key", "") or "")
    except Exception:
        pass
    api_key = api_key.strip()

    # pip_size correto por ativo (XAU/JPY = 0.01, majors = 0.0001) — quem chama
    # via CLI nem sempre sabe/preenche; o alerta/robô não pode usar o padrão errado.
    if pip_size <= 0:
        pip_size = 0.01 if any(k in symbol.upper() for k in ("XAU", "JPY")) else 0.0001

    try:
        df = get_provider(source=source, api_key=api_key or None).get_ohlcv(
            symbol, days=days, interval=interval
        )
    except Exception as e:
        return f"❌ Erro na {source} {symbol} {interval}: {e}"
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
    )

    last_signal = int(df["Signal"].iloc[-1])
    last_close = float(df["Close"].iloc[-1])
    last_atr = float(df["ATR"].iloc[-1]) if pd.notna(df["ATR"].iloc[-1]) else 0.0

    # Stops explícitos da estratégia (ex.: Fimathe) para a execução no MT5
    _sug_sl = 0.0
    _sug_tp = 0.0
    if "Suggested_SL" in df.columns and pd.notna(df["Suggested_SL"].iloc[-1]):
        _sug_sl = float(df["Suggested_SL"].iloc[-1])
    if "Suggested_TP" in df.columns and pd.notna(df["Suggested_TP"].iloc[-1]):
        _sug_tp = float(df["Suggested_TP"].iloc[-1])

    # SMC / Liquidez Alert logic
    fvg_alert = None
    if smc_alerts and "FVG_Bull_Top" in df.columns:
        bull_top = float(df["FVG_Bull_Top"].iloc[-1]) if pd.notna(df["FVG_Bull_Top"].iloc[-1]) else None
        bear_bot = float(df["FVG_Bear_Bot"].iloc[-1]) if pd.notna(df["FVG_Bear_Bot"].iloc[-1]) else None
        
        # Margem de aproximação de 0.15% (15 pips para 1.0000)
        margin = 0.0015
        
        if bull_top and 0 <= (last_close - bull_top) / bull_top <= margin:
            fvg_alert = f"🧲 *SMC / ZONA DE LIQUIDEZ*\nPreço chegando no FVG de COMPRA (Suporte em `{bull_top:.5f}`).\nIdeal para você entrar manualmente com confirmação de price-action!"
        elif bear_bot and 0 <= (bear_bot - last_close) / bear_bot <= margin:
            fvg_alert = f"🧲 *SMC / ZONA DE LIQUIDEZ*\nPreço chegando no FVG de VENDA (Resistência em `{bear_bot:.5f}`).\nIdeal para você entrar manualmente com confirmação de price-action!"

    if fvg_alert:
        state = _load_state()
        fvg_key = f"{symbol}_fvg"
        last_fvg = state.get(fvg_key, {}).get("msg", "")
        if last_fvg != fvg_alert or force:
            send_telegram(fvg_alert)
            state[fvg_key] = {"msg": fvg_alert, "ts": datetime.now().isoformat()}
            _save_state(state)

    # notifica o Telegram se o sinal mudou
    msg = notify_signal_change(symbol, last_signal, last_close, interval, force=force)

    # executa no MT5 (abre/fecha automaticamente)
    exec_msg = ""
    if execute:
        try:
            from executor import Mt5Broker
            broker = Mt5Broker()
            if broker.available():
                exec_msg = broker.on_signal(
                    symbol, last_signal, last_close, last_atr,
                    sl_atr=sl_atr, tp_atr=tp_atr, risk_pct=risk_pct,
                    suggested_sl=_sug_sl, suggested_tp=_sug_tp, pip_size=pip_size,
                )
            else:
                exec_msg = "❌ MT5 indisponível (terminal/bridge não rodando)."
        except Exception as e:
            exec_msg = f"❌ Erro ao executar no MT5: {e}"

    label = {1: "🟢 Compra", -1: "🔴 Venda", 0: "⚪ Neutro"}[last_signal]
    parts = [f"{symbol} {interval}: {label}"]
    if msg:
        parts.append("Telegram notificado")
    if execute:
        parts.append(exec_msg if exec_msg else "sem posição/sinal")
    return " | ".join(parts)


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
    parser.add_argument("--trigger-mode", default="pullback",
                        choices=["auto", "slope", "cross", "pullback", "3_9_20", "fimathe_tokio"],
                        help="Gatilho do sinal. pullback (padrão) = retorno à EMA50 no viés da tendência.")
    parser.add_argument("--pullback-ema", type=int, default=50, help="EMA usada no gatilho pullback")
    parser.add_argument("--no-volume", action="store_true")
    parser.add_argument("--session", default="Londres + Nova York (melhor janela)")
    parser.add_argument("--min-adx", type=float, default=20.0)
    parser.add_argument("--sl-atr", type=float, default=1.5)
    parser.add_argument("--tp-atr", type=float, default=3.0)
    parser.add_argument("--risk-pct", type=float, default=0.25)
    parser.add_argument("--pip-size", type=float, default=0.0001,
                        help="Valor de 1 pip do ativo (0.01 p/ XAU/JPY, 0.0001 p/ majors)")
    parser.add_argument("--fimathe-hour", type=int, default=20, help="Abertura Fimathe (0-23 UTC)")
    parser.add_argument("--fimathe-candles", type=int, default=4, help="Duração do Canal de Referência")
    parser.add_argument("--fimathe-max-pips", type=float, default=30.0, help="Fatiamento do canal em pips")
    parser.add_argument("--force", action="store_true", help="Ignora o estado salvo e notifica sempre")
    parser.add_argument("--source", default="awesomeapi", choices=["awesomeapi", "yfinance", "mt5"], 
                        help="Fonte de dados (use mt5 para execução institucional)")
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
                res = check_signal(
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
                )
                if "Neutro" not in res and "sem dados" not in res and "Erro" not in res:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {res}")
            except KeyboardInterrupt:
                print("\nRobô interrompido pelo usuário.")
                break
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Erro no loop: {e}")
            time.sleep(1)
    elif args.check:
        print(check_signal(
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
        ))
