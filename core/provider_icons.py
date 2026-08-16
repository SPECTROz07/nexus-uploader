# core/provider_icons.py
"""
Logo/favicon de cada provider, no estilo das extensoes do Mihon.

Fluxo: procura o icone declarado no HTML do site (<link rel="icon">,
apple-touch-icon, og:image), cai pro /favicon.ico e, se nada responder, desenha
um ladrilho com a inicial do provider - assim a lista nunca fica com buraco.

O resultado e gravado em assets/provider_icons/<nome>.png e reaproveitado; so
vai na rede quem ainda nao tem arquivo (ou quando forcar=True).
"""
import io
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

RAIZ = Path(__file__).resolve().parent.parent
PASTA_ICONES = RAIZ / 'assets' / 'provider_icons'
ARQUIVO_ORIGEM = PASTA_ICONES / '_origem.json'
TAMANHO = 64

# paleta pro ladrilho de fallback (mesma familia do tema Onyx)
CORES_FALLBACK = [
    (200, 169, 110), (85, 201, 138), (85, 136, 224), (224, 85, 85),
    (224, 184, 85), (150, 110, 200), (110, 190, 200), (200, 110, 160),
]

_scraper = None
_curl = None

CABECALHOS = {
    "Accept": "text/html,application/xhtml+xml,image/webp,image/avif,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def _sessao():
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False})
        _scraper.headers.update(CABECALHOS)
    return _scraper


def _sessao_curl():
    """
    Segunda tentativa pros sites que devolvem o desafio do Cloudflare pro
    cloudscraper: curl_cffi imita a impressao TLS do Chrome e passa em varios.
    """
    global _curl
    if _curl is None:
        try:
            from curl_cffi import requests as curl_requests
            _curl = curl_requests.Session(impersonate="chrome")
            _curl.headers.update(CABECALHOS)
        except Exception:
            _curl = False  # marca como indisponivel pra nao tentar de novo
    return _curl or None


def _buscar(url: str, referer: str = None):
    """GET tentando cloudscraper e, se falhar/bloquear, curl_cffi."""
    cabecalhos = {'Referer': referer} if referer else {}
    for sessao in (_sessao(), _sessao_curl()):
        if sessao is None:
            continue
        try:
            r = sessao.get(url, timeout=25, headers=cabecalhos)
            if r.status_code == 200 and b'Just a moment' not in r.content[:400]:
                return r
        except Exception:
            continue
    return None


def caminho_icone(nome: str) -> Path:
    return PASTA_ICONES / f"{nome}.png"


def _candidatos_do_html(site: str) -> list:
    """URLs de icone declaradas no <head> do site, das maiores pras menores."""
    r = _buscar(site)
    if r is None:
        return []

    soup = BeautifulSoup(r.content, 'html.parser')
    achados = []

    for link in soup.find_all('link', rel=True):
        rel = " ".join(link.get('rel')).lower()
        if 'icon' not in rel:
            continue
        href = link.get('href')
        if not href:
            continue
        # sizes="180x180" -> 180, pra priorizar o maior
        tam = 0
        m = re.match(r'(\d+)x\d+', (link.get('sizes') or ''))
        if m:
            tam = int(m.group(1))
        elif 'apple-touch' in rel:
            tam = 180
        achados.append((tam, urljoin(site, href)))

    og = soup.find('meta', attrs={'property': 'og:image'})
    if og and og.get('content'):
        achados.append((0, urljoin(site, og['content'])))

    achados.sort(key=lambda x: -x[0])
    return [u for _, u in achados]


def _baixar_imagem(url: str, referer: str) -> Optional[Image.Image]:
    r = _buscar(url, referer)
    if r is None:
        return None
    try:
        if len(r.content) < 60:
            return None
        img = Image.open(io.BytesIO(r.content))
        # .ico traz varios tamanhos; o PIL abre no maior se pedir
        if getattr(img, 'n_frames', 1) > 1:
            try:
                img.seek(img.n_frames - 1)
            except Exception:
                pass
        return img.convert('RGBA')
    except Exception:
        return None


def _ladrilho(nome: str) -> Image.Image:
    """Icone gerado com a inicial, pra provider sem favicon acessivel."""
    cor = CORES_FALLBACK[sum(ord(c) for c in nome) % len(CORES_FALLBACK)]
    img = Image.new('RGBA', (TAMANHO, TAMANHO), cor + (255,))
    desenho = ImageDraw.Draw(img)

    letra = (nome[:1] or '?').upper()
    try:
        fonte = ImageFont.truetype("segoeuib.ttf", int(TAMANHO * 0.55))
    except Exception:
        fonte = ImageFont.load_default()

    caixa = desenho.textbbox((0, 0), letra, font=fonte)
    x = (TAMANHO - (caixa[2] - caixa[0])) / 2 - caixa[0]
    y = (TAMANHO - (caixa[3] - caixa[1])) / 2 - caixa[1]
    desenho.text((x, y), letra, font=fonte, fill=(255, 255, 255, 235))
    return img


def _ajustar(img: Image.Image) -> Image.Image:
    """Deixa quadrado e no tamanho padrao, sem distorcer."""
    largura, altura = img.size
    lado = max(largura, altura)
    if largura != altura:
        base = Image.new('RGBA', (lado, lado), (0, 0, 0, 0))
        base.paste(img, ((lado - largura) // 2, (lado - altura) // 2), img)
        img = base
    return img.resize((TAMANHO, TAMANHO), Image.LANCZOS)


def ler_origem() -> dict:
    """{nome: 'site'|'fallback'} - de onde veio o icone de cada provider."""
    if not ARQUIVO_ORIGEM.exists():
        return {}
    try:
        return json.loads(ARQUIVO_ORIGEM.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _gravar_origem(nome: str, origem: str) -> None:
    dados = ler_origem()
    dados[nome] = origem
    PASTA_ICONES.mkdir(parents=True, exist_ok=True)
    ARQUIVO_ORIGEM.write_text(
        json.dumps(dados, indent=2, ensure_ascii=False, sort_keys=True), encoding='utf-8')


def obter_icone(nome: str, site: Optional[str], forcar: bool = False) -> Path:
    """
    Devolve o caminho do PNG do provider, baixando se necessario.
    Nunca levanta excecao nem devolve None - no pior caso gera o ladrilho.
    """
    PASTA_ICONES.mkdir(parents=True, exist_ok=True)
    destino = caminho_icone(nome)

    if destino.exists() and not forcar:
        return destino

    imagem = None
    if site:
        for url in _candidatos_do_html(site) + [urljoin(site, '/favicon.ico')]:
            imagem = _baixar_imagem(url, site)
            if imagem and min(imagem.size) >= 16:
                break
            imagem = None

    origem = 'site' if imagem is not None else 'fallback'
    if imagem is None:
        imagem = _ladrilho(nome)

    _ajustar(imagem).save(destino, 'PNG')
    _gravar_origem(nome, origem)
    return destino


def eh_generico(nome: str) -> bool:
    """True se o icone e o ladrilho de letra, nao a logo do site."""
    return ler_origem().get(nome) == 'fallback'


def tem_icone_proprio(nome: str) -> bool:
    """True se o arquivo existe (nao diz se veio do site ou do fallback)."""
    return caminho_icone(nome).exists()
