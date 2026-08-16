#!/usr/bin/env python3
"""
Launcher do Nexus Upload: atualiza pelo Git e abre o app.

A ideia: em vez de reempacotar e subir um instalador de centenas de MB a cada
mexida, o codigo vem do repositorio privado. Um `git pull` traz so o delta dos
arquivos que mudaram - normalmente alguns KB.

O que ele faz a cada abertura:
  1. git pull no repositorio (se houver rede; sem rede, abre a versao local)
  2. se requirements.txt mudou desde a ultima vez, roda pip install
  3. se faltar navegador do Playwright, instala
  4. abre o main_gui.py com o python do venv

Nada aqui apaga trabalho local: se o pull encontrar conflito com arquivo
alterado na maquina, o launcher avisa e segue com o que esta em disco, em vez
de descartar mudanca de alguem.

Uso:
    pythonw launcher.py                 abre normalmente
    python  launcher.py --console       mostra o log da atualizacao
    python  launcher.py --pular         abre sem tentar atualizar
    python  launcher.py --so-atualizar  atualiza e NAO abre o app
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / ".launcher_estado.json"
VENV = RAIZ / "venv"
TIMEOUT_GIT = 90

CONSOLE = "--console" in sys.argv or "--so-atualizar" in sys.argv
PULAR = "--pular" in sys.argv
SO_ATUALIZAR = "--so-atualizar" in sys.argv


def log(msg: str) -> None:
    if CONSOLE:
        print(msg, flush=True)


def python_do_venv() -> Path:
    return VENV / "Scripts" / ("python.exe" if os.name == "nt" else "python")


def pythonw_do_venv() -> Path:
    p = VENV / "Scripts" / "pythonw.exe"
    return p if p.exists() else python_do_venv()


def rodar(args, **kw):
    """Executa um comando escondendo a janela de console no Windows."""
    flags = 0
    if os.name == "nt" and not CONSOLE:
        flags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        args, cwd=str(RAIZ), capture_output=True, text=True,
        encoding="utf-8", errors="ignore", creationflags=flags, **kw)


def ler_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def gravar_estado(dados: dict) -> None:
    try:
        ESTADO.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    except Exception:
        pass


def hash_requirements() -> str:
    arq = RAIZ / "requirements.txt"
    if not arq.exists():
        return ""
    return hashlib.sha256(arq.read_bytes()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# atualizacao
# ─────────────────────────────────────────────────────────────────────────────

def tem_git() -> bool:
    try:
        return rodar(["git", "--version"], timeout=15).returncode == 0
    except Exception:
        return False


def atualizar_via_credenciais_salvas() -> str | None:
    """
    Se o app tiver credenciais de git salvas (repo privado + token), usa elas.
    Devolve o status do GitUpdater, ou None se nao houver config (cai no fluxo
    padrao de git pull).
    """
    try:
        sys.path.insert(0, str(RAIZ))
        from core.git_updater import GitUpdater
        gu = GitUpdater(RAIZ)
        if not gu.is_configured():
            return None
        gu.ensure_remote()
        status, mensagem, commits = gu.check_and_pull()
        log(f"[git] {status}: {mensagem}")
        for c in commits[:15]:
            log(f"   {c}")
        return status
    except Exception as e:
        log(f"[git] atualizacao por credenciais falhou: {e}")
        return None


def atualizar_codigo() -> str:
    """
    Faz o pull. Devolve 'atualizado', 'em-dia', 'sem-rede', 'local-sujo' ou 'erro'.
    """
    if not (RAIZ / ".git").exists():
        log("Nao e um clone do repositorio - atualizacao ignorada.")
        return "erro"

    if not tem_git():
        log("Git nao encontrado no PATH - abrindo a versao local.")
        return "erro"

    # 1) repo privado com credenciais salvas no app
    via_cred = atualizar_via_credenciais_salvas()
    if via_cred is not None:
        return via_cred

    # 2) fallback: pull cru (repo publico ou credencial no git credential manager)
    antes = rodar(["git", "rev-parse", "HEAD"], timeout=TIMEOUT_GIT).stdout.strip()

    # arquivo alterado na maquina impede o pull; melhor avisar do que sobrescrever
    sujo = rodar(["git", "status", "--porcelain"], timeout=TIMEOUT_GIT).stdout.strip()
    if sujo:
        linhas = [l for l in sujo.splitlines() if l.strip()]
        log(f"Ha {len(linhas)} arquivo(s) alterado(s) localmente:")
        for l in linhas[:10]:
            log(f"   {l}")
        log("Pull adiado pra nao descartar essas mudancas.")
        return "local-sujo"

    r = rodar(["git", "pull", "--ff-only"], timeout=TIMEOUT_GIT)
    if r.returncode != 0:
        saida = (r.stderr or r.stdout or "").lower()
        if any(x in saida for x in ("could not resolve", "unable to access",
                                    "timed out", "network")):
            log("Sem rede - abrindo a versao local.")
            return "sem-rede"
        log(f"git pull falhou:\n{r.stderr or r.stdout}")
        return "erro"

    depois = rodar(["git", "rev-parse", "HEAD"], timeout=TIMEOUT_GIT).stdout.strip()
    if antes and depois and antes != depois:
        resumo = rodar(
            ["git", "log", "--oneline", f"{antes}..{depois}"], timeout=TIMEOUT_GIT
        ).stdout.strip()
        log("Atualizado:")
        for linha in resumo.splitlines()[:15]:
            log(f"   {linha}")
        return "atualizado"

    log("Ja esta na ultima versao.")
    return "em-dia"


def garantir_dependencias(forcar: bool = False) -> None:
    """pip install so quando requirements.txt muda - senao cada abertura demora."""
    estado = ler_estado()
    atual = hash_requirements()
    if not forcar and estado.get("hash_requirements") == atual:
        return

    py = python_do_venv()
    if not py.exists():
        log("venv ausente - rode o install.ps1 primeiro.")
        return

    log("requirements.txt mudou - instalando dependencias...")
    r = rodar([str(py), "-m", "pip", "install", "-r",
               str(RAIZ / "requirements.txt"), "--disable-pip-version-check"],
              timeout=1800)
    if r.returncode == 0:
        estado["hash_requirements"] = atual
        gravar_estado(estado)
        log("Dependencias em dia.")
    else:
        log(f"pip install falhou:\n{(r.stderr or r.stdout)[-1500:]}")


def garantir_navegadores() -> None:
    """Baixa o Chromium do Playwright na primeira vez (fica fora do repo)."""
    pasta = RAIZ / "playwright_browsers"
    if pasta.exists() and any(pasta.glob("chromium-*")):
        return

    py = python_do_venv()
    if not py.exists():
        return

    log("Baixando navegador do Playwright (so na primeira vez)...")
    ambiente = os.environ.copy()
    ambiente["PLAYWRIGHT_BROWSERS_PATH"] = str(pasta)
    r = rodar([str(py), "-m", "playwright", "install", "chromium"],
              timeout=1800, env=ambiente)
    if r.returncode != 0:
        log("Nao consegui instalar o navegador; providers com Playwright podem falhar.")


# ─────────────────────────────────────────────────────────────────────────────

def abrir_app() -> int:
    py = pythonw_do_venv() if not CONSOLE else python_do_venv()
    if not py.exists():
        log("venv nao encontrado. Rode install.ps1 antes.")
        return 1

    alvo = RAIZ / "main_gui.py"
    if not alvo.exists():
        log("main_gui.py nao encontrado.")
        return 1

    ambiente = os.environ.copy()
    pasta_navegadores = RAIZ / "playwright_browsers"
    if pasta_navegadores.exists():
        ambiente["PLAYWRIGHT_BROWSERS_PATH"] = str(pasta_navegadores)

    log("Abrindo o Nexus Upload...")
    if CONSOLE:
        return subprocess.call([str(py), str(alvo)], cwd=str(RAIZ), env=ambiente)

    subprocess.Popen([str(py), str(alvo)], cwd=str(RAIZ), env=ambiente,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return 0


def main() -> int:
    inicio = time.time()
    log(f"Nexus Upload - launcher ({RAIZ})")

    if not PULAR:
        resultado = atualizar_codigo()
        garantir_dependencias(forcar=(resultado == "atualizado"))
        garantir_navegadores()
        estado = ler_estado()
        estado["ultima_checagem"] = time.strftime("%Y-%m-%d %H:%M:%S")
        estado["ultimo_resultado"] = resultado
        gravar_estado(estado)

    log(f"Pronto em {time.time() - inicio:.1f}s")

    if SO_ATUALIZAR:
        log("--so-atualizar: nao vou abrir o app.")
        return 0

    return abrir_app()


if __name__ == "__main__":
    sys.exit(main())
