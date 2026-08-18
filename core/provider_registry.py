# core/provider_registry.py
"""
Inventario dos providers instalados, lido ESTATICAMENTE do codigo-fonte.

Por que nao importar/instanciar: 20 dos 46 providers sobem undetected_chromedriver
ou Playwright no __init__. Montar uma lista pra UI abrindo 20 navegadores esta
fora de cogitacao - entao a leitura e feita com `ast`, sem executar nada.

Cada provider vira um ProviderInfo com nome (o que o load_provider usa), classe,
arquivo e o site (base_url), que e o que permite buscar o favicon.
"""
import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# classes que sao motor compartilhado, nao provider de site
BASES = {"BaseProvider", "ScanApiProvider"}


@dataclass
class ProviderInfo:
    nome: str           # 'maidscan' -> providers/maidscan_provider.py
    classe: str         # 'MaidscanProvider'
    arquivo: Path
    site: Optional[str] = None   # https://empreguetes.wtf
    herda_de: Optional[str] = None
    # declarados pelo provider como atributos de classe (literais, lidos por
    # ast/manifest — a UI monta os botões sem importar o módulo):
    #   LOGIN_ARQUIVO = "rfdragon_login.json"  (+ LOGIN_CAMPOS opcional)
    #   SESSAO_CF = "lycantoons.com"  (chave no session_manager)
    login_arquivo: Optional[str] = None
    login_campos: Optional[tuple] = None
    sessao_cf: Optional[str] = None

    @property
    def dominio(self) -> Optional[str]:
        if not self.site:
            return None
        m = re.match(r'https?://([^/]+)', self.site)
        return m.group(1) if m else None

    @property
    def nome_exibicao(self) -> str:
        """Nome da classe sem o sufixo Provider ('MaidscanProvider' -> 'Maidscan')."""
        return re.sub(r'Provider$', '', self.classe) or self.nome

    @property
    def site_publico(self) -> Optional[str]:
        """
        Site onde mora o favicon. Varios providers so guardam a URL do backend
        (api.site.com, api2.site.com/algo) - o icone nao vive la, entao o
        subdominio de API e o caminho sao removidos.
        """
        if not self.site:
            return None
        m = re.match(r'(https?://)([^/]+)', self.site)
        if not m:
            return None
        esquema, host = m.group(1), m.group(2)
        host_limpo = re.sub(r'^(api\d*|backend|cdn|media)\.', '', host, flags=re.I)
        return f"{esquema}{host_limpo}"


def _texto_de(no) -> Optional[str]:
    """Devolve a string literal de um no do ast, se for uma."""
    if isinstance(no, ast.Constant) and isinstance(no.value, str):
        return no.value
    return None


# URLs que nao representam o site do provider: proxy FlareSolverr, servidor local
HOSTS_IGNORADOS = re.compile(
    r'^(localhost|127\.|0\.0\.0\.0|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)', re.I)


def _url_do_no(no) -> Optional[str]:
    txt = _texto_de(no)
    if not txt or not txt.startswith(('http://', 'https://')):
        return None
    host = re.sub(r'^https?://', '', txt).split('/')[0]
    if HOSTS_IGNORADOS.match(host) or host.endswith(':8191'):
        return None
    return txt.rstrip('/')


def _extrair_site(classe: ast.ClassDef, arvore: ast.Module) -> Optional[str]:
    """
    Procura a URL do site em tres lugares, nesta ordem:
      1. atributo de classe  ->  base_url = "https://..."
      2. atribuicao no init  ->  self.base_url = "https://..."
      3. qualquer literal http no modulo (ultimo recurso)
    """
    # 1. atributo de classe
    for no in classe.body:
        if isinstance(no, ast.Assign):
            for alvo in no.targets:
                if isinstance(alvo, ast.Name) and alvo.id in ('base_url', 'api_url'):
                    url = _url_do_no(no.value)
                    if url:
                        return url
        elif isinstance(no, ast.AnnAssign):
            if isinstance(no.target, ast.Name) and no.target.id == 'base_url' and no.value:
                url = _url_do_no(no.value)
                if url:
                    return url

    # 2. self.base_url dentro de qualquer metodo (normalmente __init__)
    for no in ast.walk(classe):
        if isinstance(no, ast.Assign):
            for alvo in no.targets:
                if (isinstance(alvo, ast.Attribute)
                        and alvo.attr in ('base_url', 'site_url')
                        and isinstance(alvo.value, ast.Name)
                        and alvo.value.id == 'self'):
                    url = _url_do_no(no.value)
                    if url:
                        return url

    # 3. primeiro literal http do arquivo que nao seja de CDN/servico
    for no in ast.walk(arvore):
        url = _url_do_no(no)
        if url and not re.search(r'(googleapis|gstatic|w3\.org|schema\.org|github)', url):
            return url

    return None


def _declaracoes_da_classe(classe: ast.ClassDef) -> dict:
    """
    Lê os atributos declarativos (LOGIN_ARQUIVO, LOGIN_CAMPOS, SESSAO_CF)
    do corpo da classe — só literais simples, sem executar nada.
    """
    out = {}
    for no in classe.body:
        if not isinstance(no, ast.Assign):
            continue
        for alvo in no.targets:
            if not isinstance(alvo, ast.Name):
                continue
            if alvo.id in ('LOGIN_ARQUIVO', 'SESSAO_CF'):
                txt = _texto_de(no.value)
                if txt:
                    out[alvo.id] = txt
            elif alvo.id == 'LOGIN_CAMPOS' and isinstance(no.value, (ast.Tuple, ast.List)):
                campos = [_texto_de(e) for e in no.value.elts]
                if campos and all(campos):
                    out[alvo.id] = tuple(campos)
    return out


def ler_provider(arquivo: Path) -> Optional[ProviderInfo]:
    """Le um providers/<nome>_provider.py sem importar."""
    nome = arquivo.stem.replace('_provider', '')
    try:
        arvore = ast.parse(arquivo.read_text(encoding='utf-8', errors='ignore'))
    except SyntaxError:
        return None

    # o load_provider do run_bot monta o nome capitalizando cada parte
    esperada = "".join(p.capitalize() for p in nome.split('_')) + "Provider"

    candidatas = [n for n in ast.walk(arvore)
                  if isinstance(n, ast.ClassDef)
                  and n.name.endswith('Provider')
                  and n.name not in BASES]
    if not candidatas:
        return None

    classe = next((c for c in candidatas if c.name == esperada), candidatas[0])
    herda = next((b.id for b in classe.bases if isinstance(b, ast.Name)), None)

    decl = _declaracoes_da_classe(classe)
    return ProviderInfo(
        nome=nome,
        classe=classe.name,
        arquivo=arquivo,
        site=_extrair_site(classe, arvore),
        herda_de=herda,
        login_arquivo=decl.get('LOGIN_ARQUIVO'),
        login_campos=decl.get('LOGIN_CAMPOS'),
        sessao_cf=decl.get('SESSAO_CF'),
    )


def _carregar_manifesto(pasta: Path) -> dict:
    """
    manifest.json (gerado pelo build_providers.py) guarda os metadados que o
    .pyd compilado não deixa ler por ast: {nome: {classe, site, rotulo}}.
    Só é consultado para providers que existem como .pyd sem .py ao lado.
    """
    import json
    arq = pasta / "manifest.json"
    if not arq.exists():
        return {}
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _info_do_manifesto(nome: str, arquivo: Path, dados: dict) -> ProviderInfo:
    classe = dados.get("classe") or nome_da_classe_esperada(nome)
    campos = dados.get("login_campos")
    return ProviderInfo(
        nome=nome, classe=classe, arquivo=arquivo,
        site=dados.get("site"), herda_de=dados.get("herda_de"),
        login_arquivo=dados.get("login_arquivo"),
        login_campos=tuple(campos) if campos else None,
        sessao_cf=dados.get("sessao_cf"),
    )


def listar_providers(pasta: Path = None) -> List[ProviderInfo]:
    """
    Todos os providers instalados, em ordem alfabetica. Cobre os dois formatos:
    *_provider.py (fonte, lido por ast) e *_provider.pyd (compilado, lido do
    manifest.json). Se os dois existirem pro mesmo nome, o .py vence (dev local).
    """
    from core.paths import pasta_providers
    pasta = pasta or pasta_providers()
    manifesto = _carregar_manifesto(pasta)
    achados = {}

    for arquivo in sorted(pasta.glob('*_provider.py')):
        if arquivo.stem.startswith('_'):
            continue
        info = ler_provider(arquivo)
        if info:
            achados[info.nome] = info

    for arquivo in sorted(pasta.glob('*_provider*.pyd')):
        # Nuitka pode nomear como maidscan_provider.cp313-win_amd64.pyd
        nome = arquivo.name.split('_provider', 1)[0]
        if nome.startswith('_') or nome in achados:
            continue
        achados[nome] = _info_do_manifesto(nome, arquivo, manifesto.get(nome, {}))

    return [achados[n] for n in sorted(achados)]


def nome_da_classe_esperada(nome: str) -> str:
    """Mesmo calculo do run_bot.load_provider - util pra apontar divergencia."""
    return "".join(p.capitalize() for p in nome.split('_')) + "Provider"
