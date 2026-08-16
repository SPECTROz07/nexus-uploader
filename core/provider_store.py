# core/provider_store.py
"""
Sincronização de providers com o repositório GitHub, arquivo a arquivo.

O modelo de distribuição: o app quase nunca atualiza, mas os providers sim.
Em vez de exigir git clone + pull na máquina de cada uploader, a pasta
providers/ do repositório é lida pela API de conteúdo do GitHub (funciona
com repo privado via token) e comparada com a pasta local por SHA de blob —
o mesmo hash que o git usa, então "em dia" aqui significa idêntico ao repo.

Credenciais: as mesmas do GitUpdater (repo_url/token/branch cifrados em
Documents/SpectralModeração/git_update.json), configuradas uma vez na GUI.
"""
import hashlib
import re
from pathlib import Path

import requests

PASTA_REMOTA = "providers"
ARQUIVO_UPLOADERS = "uploaders.json"   # na raiz do repo: {"emails": ["a@b.com", ...]}
TIMEOUT = 30

# e-mail já verificado desta máquina (não é segredo, só identidade)
_ARQ_EMAIL_LOCAL = Path.home() / "Documents" / "SpectralModeração" / "uploader_email.json"


def _repo_api(repo_url: str) -> str:
    """https://github.com/dono/repo(.git) -> https://api.github.com/repos/dono/repo"""
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url.strip())
    if not m:
        raise ValueError(f"URL de repositório não reconhecida: {repo_url!r}")
    return f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"


def _headers(token: str, raw=False):
    h = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "NexusUploader",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _sha_de_bytes(dados: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"blob {len(dados)}\0".encode("ascii"))
    h.update(dados)
    return h.hexdigest()


def sha_blob(caminho: Path) -> str:
    """SHA1 de blob no formato do git: sha1(b'blob <len>\\0' + conteúdo)."""
    return _sha_de_bytes(Path(caminho).read_bytes())


def shas_possiveis(caminho: Path) -> set:
    """
    SHAs que o arquivo local pode ter no repo. No Windows o git normaliza
    CRLF->LF ao commitar (autocrlf), então o blob do repo pode ser o conteúdo
    com LF mesmo com o arquivo local em CRLF — sem isto, tudo que tem CRLF
    local apareceria eternamente como "atualização".
    """
    dados = Path(caminho).read_bytes()
    shas = {_sha_de_bytes(dados)}
    normalizado = dados.replace(b"\r\n", b"\n")
    if normalizado != dados:
        shas.add(_sha_de_bytes(normalizado))
    return shas


def listar_remotos(repo_url: str, token: str, branch: str = "main"):
    """Providers da pasta do repo: [{name, path, sha, size}]. Cobre tanto o repo
    de código aberto (.py) quanto o fechado (.pyd) + o manifest.json."""
    url = f"{_repo_api(repo_url)}/contents/{PASTA_REMOTA}"
    r = requests.get(url, headers=_headers(token), params={"ref": branch}, timeout=TIMEOUT)
    if r.status_code == 404:
        raise RuntimeError("Pasta 'providers/' não existe no repositório (ou branch errado).")
    if r.status_code in (401, 403):
        raise RuntimeError("Sem acesso ao repositório — confira o token nas Configurações.")
    r.raise_for_status()
    return [{"name": e["name"], "path": e["path"], "sha": e["sha"], "size": e.get("size", 0)}
            for e in r.json()
            if e.get("type") == "file"
            and (e["name"].endswith((".py", ".pyd")) or e["name"] == "manifest.json")]


def baixar_arquivo(repo_url: str, token: str, caminho_remoto: str, destino: Path, branch: str = "main"):
    """Baixa um arquivo do repo (conteúdo cru) e grava em `destino`."""
    url = f"{_repo_api(repo_url)}/contents/{caminho_remoto}"
    r = requests.get(url, headers=_headers(token, raw=True), params={"ref": branch}, timeout=TIMEOUT)
    r.raise_for_status()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(r.content)
    return destino


def emails_aprovados(repo_url: str, token: str, branch: str = "main"):
    """
    Lista de e-mails aprovados no uploaders.json da raiz do repo (minúsculos).
    Devolve None se o arquivo não existe no repo — aí NÃO há trava de acesso.
    """
    import json as _json
    url = f"{_repo_api(repo_url)}/contents/{ARQUIVO_UPLOADERS}"
    r = requests.get(url, headers=_headers(token, raw=True), params={"ref": branch}, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise RuntimeError("Sem acesso ao repositório — confira o token nas Configurações.")
    r.raise_for_status()
    dados = _json.loads(r.content.decode("utf-8"))
    lista = dados.get("emails", dados) if isinstance(dados, dict) else dados
    return [str(e).strip().lower() for e in lista if str(e).strip()]


def email_salvo() -> str:
    import json as _json
    try:
        return _json.loads(_ARQ_EMAIL_LOCAL.read_text(encoding="utf-8")).get("email", "")
    except Exception:
        return ""


def salvar_email(email: str):
    import json as _json
    _ARQ_EMAIL_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_EMAIL_LOCAL.write_text(_json.dumps({"email": (email or "").strip()}), encoding="utf-8")


def verificar_acesso(repo_url: str, token: str, branch: str = "main", email: str = None):
    """
    (liberado, motivo). Usa o e-mail salvo quando nenhum é passado.
    Sem uploaders.json no repo = liberado (o dono ainda não ativou a trava).
    Falha de rede = negado (fail-closed) com o motivo.
    """
    email = (email or email_salvo() or "").strip().lower()
    try:
        lista = emails_aprovados(repo_url, token, branch)
    except Exception as e:
        return False, f"Não deu pra checar a lista de uploaders: {e}"
    if lista is None or len(lista) == 0:
        # sem arquivo OU lista vazia = sem trava de e-mail (o login já autentica)
        return True, "sem trava de e-mail (allowlist vazia)"
    if not email:
        return False, "Nenhum e-mail verificado nesta máquina."
    if email in lista:
        return True, "e-mail aprovado"
    return False, f"O e-mail '{email}' não está na lista de uploaders aprovados."


def comparar(pasta_local: Path, remotos):
    """
    Cruza local × repo. Devolve lista de dicts ordenada por relevância:
      {"name", "estado": novo|atualizacao|em-dia|so-local, "remoto": {...}|None}
    __init__.py e afins do pacote entram na comparação normalmente (podem
    atualizar), mas "so-local" só é reportado pra *_provider.py — o resto do
    conteúdo local (caches, __pycache__) não interessa.
    """
    pasta_local = Path(pasta_local)
    por_nome = {r["name"]: r for r in remotos}
    resultado = []

    for remoto in remotos:
        local = pasta_local / remoto["name"]
        if not local.exists():
            resultado.append({"name": remoto["name"], "estado": "novo", "remoto": remoto})
        elif remoto["sha"] not in shas_possiveis(local):
            resultado.append({"name": remoto["name"], "estado": "atualizacao", "remoto": remoto})
        else:
            resultado.append({"name": remoto["name"], "estado": "em-dia", "remoto": remoto})

    for padrao in ("*_provider.py", "*_provider*.pyd"):
        for local in sorted(pasta_local.glob(padrao)):
            if local.name not in por_nome:
                resultado.append({"name": local.name, "estado": "so-local", "remoto": None})

    ordem = {"novo": 0, "atualizacao": 1, "so-local": 2, "em-dia": 3}
    resultado.sort(key=lambda x: (ordem[x["estado"]], x["name"]))
    return resultado
