"""
Infraestrutura de segurança para operação AUTOMÁTICA em conta real.

Três coisas que faltavam e que são a diferença entre "robô" e "robô suicida":

1. KILL-SWITCH — arquivo `TRADING_DISABLED` na raiz do projeto. Se existir,
   NINGUÉM abre posição (nem o cron, nem o app, nem o loop contínuo). Pode ser
   criado à mão, por script ou automaticamente quando o limite diário de perda
   estoura. `python safety.py status|enable|disable "motivo"`.

2. LOCK — um único executor por vez. O cron roda `run_alerts.sh` e
   `run_trader.sh` no MESMO minuto e o app executa de 10 em 10 s; sem lock,
   dois processos leem "sem posição" e abrem a mesma ordem duas vezes.

3. JSON ATÔMICO — os arquivos de estado eram abertos em modo 'w' (trunca antes
   de gravar) com `except: pass`. Uma queda no meio da escrita apagava capital e
   histórico em silêncio. Agora grava em .tmp + os.replace (atômico no POSIX).
"""

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KILL_SWITCH_FILE = os.path.join(BASE_DIR, "TRADING_DISABLED")
LOCK_FILE = "/tmp/mercado_trader.exec.lock"
LOCK_TIMEOUT_S = 120.0


# ────────────────────────────────────────────────────────────── kill-switch
def kill_switch_reason() -> str:
    """Devolve o motivo da desativação, ou string vazia se o trading está liberado."""
    try:
        with open(KILL_SWITCH_FILE, "r", encoding="utf-8") as f:
            return (f.read() or "desativado").strip() or "desativado"
    except FileNotFoundError:
        return ""
    except OSError as e:
        # Não dá para LER o estado => não se sabe se está ligado ou desligado.
        # Em modo real o padrão seguro é BLOQUEAR, não operar às cegas.
        return f"kill-switch ilegível ({e})"


def disable_trading(reason: str) -> str:
    """Liga o kill-switch. Retorna o motivo gravado."""
    reason = (reason or "desativado").strip() or "desativado"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write_text(KILL_SWITCH_FILE, f"{reason}\n(desativado em {stamp})\n")
    return reason


def enable_trading() -> None:
    try:
        os.remove(KILL_SWITCH_FILE)
    except FileNotFoundError:
        pass


def trading_allowed() -> tuple:
    """(permitido: bool, motivo: str)."""
    reason = kill_switch_reason()
    return (not reason), reason


# ─────────────────────────────────────────────────────────────────── lock
def _try_lock(fd) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        return e.errno in (errno.EAGAIN, errno.EACCES)


@contextmanager
def execution_lock(blocking: bool = True, timeout: float = LOCK_TIMEOUT_S):
    """
    Lock exclusivo de execução. Uso:

        with execution_lock() as got:
            if not got:
                return "já existe outro executor rodando"

    `blocking=False` (ou timeout=0) faz lock não-bloqueante: quem chega depois
    desiste na hora em vez de enfileirar uma ordem atrasada.
    """
    import time
    fd = open(LOCK_FILE, "a+")
    got = False
    try:
        if blocking and timeout > 0:
            deadline = time.time() + timeout
            while not _try_lock(fd):
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
            else:
                got = True
            if not got:
                got = _try_lock(fd)
        else:
            got = _try_lock(fd)
        yield got
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


# ──────────────────────────────────────────────────────── JSON atômico
def atomic_write_json(path: str, data, default=None) -> None:
    """Grava JSON de forma atômica — nunca deixa o arquivo pela metade."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False,
                      default=default or str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: str, default):
    """
    Lê JSON tolerando corrupção, SEM descartar o dado.

    Diferença crucial em relação ao `except: return {}` antigo: se o arquivo
    estiver ilegível, renomeia para `.corrupt-<ts>` e devolve o default — mas
    um arquivo VÁLIDO nunca é tratado como vazio (o PaperBroker reiniciava o
    capital e apagava o histórico do nada).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, ValueError):
        try:
            os.replace(path, f"{path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        except OSError:
            pass
        return default


def warn(message: str) -> None:
    """Log consistente — nada de `except: pass` sem rastro."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ {message}", flush=True)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        allowed, reason = trading_allowed()
        print("TRADING LIGADO" if allowed else f"TRADING DESLIGADO — {reason}")
    elif cmd == "disable":
        print("desativado:", disable_trading(" ".join(sys.argv[2:]) or "manual"))
    elif cmd == "enable":
        enable_trading()
        print("trading reativado")
    else:
        print("uso: python safety.py status|enable|disable [motivo]")
