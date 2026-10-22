"""
Adiciona os símbolos usados ao Market Watch do MT5 (e ativa o streaming).

Sem isso, símbolos como XAUUSD não aparecem/abrem no gráfico do terminal
porque ficam fora do Market Watch (visible=False, sem cotação). Rode após o
bridge subir (o run_app.sh chama isso no boot).

Uso: python3 ensure_symbols.py
"""

from mt5_bridge import MT5Bridge

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "US500", "NAS100", "US30", "GER40", "IBOV"]


def main():
    m = MT5Bridge()
    if not m.available():
        print("❌ Bridge não está rodando (mt5server.exe).")
        return
    m.initialize()
    for sym in SYMBOLS:
        ok = m._eval(f"mt5.symbol_select('{sym}', True)")
        q = m.quote(sym)
        state = "streamando" if q.get("bid") else "sem cotação"
        print(f"{sym}: select={bool(ok)} | {state}")
    print("OK. Agora é só abrir o gráfico no MT5 (Ctrl+U → XAUUSD → Add/Show).")


if __name__ == "__main__":
    main()
