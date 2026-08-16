# core/git_updater.py
"""
Atualização do app pelo Git, com credenciais salvas (criptografadas).

Para repositório PRIVADO o GitHub não autentica por email/senha — precisa de um
Personal Access Token (PAT). Aqui guardamos repo_url + email/usuário + token
(cifrados com a mesma chave Fernet do CredentialsManager) e usamos isso pra
puxar as atualizações sem pedir nada ao usuário.

O token NÃO é persistido no .git/config: ele entra só na hora do pull, numa URL
autenticada montada em memória, e é removido de qualquer log/saída de erro.
"""
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / "Documents" / "SpectralModeração"
CONFIG_FILE = CONFIG_DIR / "git_update.json"

# Repo público de distribuição — o uploader não configura NADA: a URL vem
# daqui e a permissão é o e-mail dele no uploaders.json do repo (email_gate).
# Token é opcional (só faria sentido se o repo voltasse a ser privado).
DEFAULT_REPO_URL = "https://github.com/SPECTROz07/nexus-uploader"
DEFAULT_BRANCH = "main"


class GitUpdater:
    def __init__(self, repo_dir=None):
        self.repo_dir = Path(repo_dir) if repo_dir else RAIZ
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from core.master_key import fernet as _mk_fernet
        self.fernet = _mk_fernet()  # chave-mestra centralizada (DPAPI)

    def _enc(self, s: str) -> str:
        return self.fernet.encrypt((s or "").encode("utf-8")).decode("utf-8")

    def _dec(self, s: str) -> str:
        if not s:
            return ""
        try:
            return self.fernet.decrypt(s.encode("utf-8")).decode("utf-8")
        except Exception:
            return ""

    # --- config ---------------------------------------------------------
    def save_config(self, repo_url, email, token, branch="main", auto=True):
        data = {
            "repo_url": self._enc(repo_url),
            "email": self._enc(email),
            "token": self._enc(token),
            "branch": (branch or "main").strip(),
            "auto": bool(auto),
        }
        CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")

    def load_config(self) -> dict:
        vazio = {"repo_url": DEFAULT_REPO_URL, "email": "", "token": "",
                 "branch": DEFAULT_BRANCH, "auto": True}
        if not CONFIG_FILE.exists():
            return vazio
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return vazio
        return {
            "repo_url": self._dec(data.get("repo_url", "")) or DEFAULT_REPO_URL,
            "email": self._dec(data.get("email", "")),
            "token": self._dec(data.get("token", "")),
            "branch": data.get("branch", DEFAULT_BRANCH) or DEFAULT_BRANCH,
            "auto": data.get("auto", True),
        }

    def is_configured(self) -> bool:
        """Repo público como padrão: sempre há de onde puxar; token é opcional."""
        return bool(self.load_config().get("repo_url"))

    def _tem_remote(self) -> bool:
        try:
            return bool(self._run(["remote", "get-url", "origin"], timeout=15).stdout.strip())
        except Exception:
            return False

    def pode_atualizar(self) -> bool:
        """
        Dá pra puxar se:
          - é um clone git E (já tem um 'origin' remoto  OU  repo+token salvos).
        O caso do 'origin' cobre o modelo colaborador: a máquina clonou logando
        uma vez, o Git guardou a credencial, e o pull nem precisa de token no app.
        """
        if not (self.repo_dir / ".git").exists():
            return False
        return self._tem_remote() or self.is_configured()

    # --- git ------------------------------------------------------------
    def _run(self, args, timeout=90):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(
            ["git"] + args, cwd=str(self.repo_dir),
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            creationflags=flags, timeout=timeout,
        )

    def _tem_git(self) -> bool:
        try:
            return self._run(["--version"], timeout=15).returncode == 0
        except Exception:
            return False

    @staticmethod
    def _auth_url(repo_url, email, token) -> str:
        m = re.match(r"https?://(.*)", repo_url.strip())
        host_path = m.group(1) if m else repo_url.strip()
        host_path = re.sub(r"^[^@/]*@", "", host_path)  # tira credencial pré-existente
        user = email.strip() or "x-access-token"
        return f"https://{quote(user, safe='')}:{quote(token, safe='')}@{host_path}"

    def ensure_remote(self):
        """Garante origin apontando pra repo_url (sem token no config)."""
        c = self.load_config()
        if not c.get("repo_url"):
            return
        atual = self._run(["remote", "get-url", "origin"]).stdout.strip()
        if not atual:
            self._run(["remote", "add", "origin", c["repo_url"]])
        elif atual != c["repo_url"]:
            self._run(["remote", "set-url", "origin", c["repo_url"]])

    def check_and_pull(self):
        """
        Devolve (status, mensagem, commits[]).
        status ∈ atualizado | em-dia | nao-config | sem-git | local-sujo |
                 auth | sem-rede | sem-acesso | erro
        """
        c = self.load_config()
        if not c.get("repo_url"):
            return "nao-config", "Atualização por Git não configurada.", []
        if not (self.repo_dir / ".git").exists():
            return "sem-git", "A pasta do app não é um clone git.", []
        if not self._tem_git():
            return "erro", "Git não encontrado no PATH da máquina.", []

        # Portão de uploader: o repo pode carregar um uploaders.json com os
        # e-mails aprovados; sem e-mail verificado nesta máquina, nada de pull.
        from core import provider_store
        liberado, motivo = provider_store.verificar_acesso(
            c["repo_url"], c.get("token", ""), c.get("branch", "main"))
        if not liberado:
            return "sem-acesso", motivo, []

        antes = self._run(["rev-parse", "HEAD"]).stdout.strip()

        sujo = self._run(["status", "--porcelain"]).stdout.strip()
        if sujo:
            n = len([l for l in sujo.splitlines() if l.strip()])
            return "local-sujo", f"{n} arquivo(s) alterado(s) localmente; pull adiado.", []

        branch = c.get("branch") or "main"
        # repo público dispensa autenticação; token (se houver) segue suportado
        if c.get("token"):
            url = self._auth_url(c["repo_url"], c.get("email", ""), c["token"])
        else:
            url = c["repo_url"]
        r = self._run(["pull", "--ff-only", url, branch], timeout=180)
        saida = (r.stderr or r.stdout or "")
        if c.get("token"):
            saida = saida.replace(c["token"], "***")

        if r.returncode != 0:
            low = saida.lower()
            if any(x in low for x in ("authentication", "denied", "403", "invalid username")):
                return "auth", "Falha de autenticação — confira email/token.", []
            if any(x in low for x in ("could not resolve", "unable to access", "timed out", "network")):
                return "sem-rede", "Sem conexão com o GitHub.", []
            return "erro", saida.strip()[:300] or "Falha no git pull.", []

        depois = self._run(["rev-parse", "HEAD"]).stdout.strip()
        if antes and depois and antes != depois:
            commits = self._run(["log", "--oneline", f"{antes}..{depois}"]).stdout.strip().splitlines()
            return "atualizado", f"{len(commits)} commit(s) novo(s) baixado(s).", commits
        return "em-dia", "Já está na última versão.", []
