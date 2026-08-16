# core/provider_health.py
"""
Sonda de saúde dos sites dos providers, compartilhada entre o CLI
(provider-health.py) e a aba Providers da GUI.

O resultado de cada varredura fica cacheado em assets/provider_health.json —
a aba mostra o último estado conhecido sem precisar sondar de novo a cada
abertura; sondar é ação explícita do usuário.
"""
import json
import re
import time
from pathlib import Path

import cloudscraper

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

from core.paths import pasta_assets
ARQUIVO_CACHE = pasta_assets() / "provider_health.json"

# status → (rótulo curto pra UI, gravidade: erro/aviso/ok)
ROTULOS = {
    "morto": ("site fora", "erro"),
    "mudou": ("mudou de domínio", "aviso"),
    "bloqueado": ("Cloudflare", "aviso"),
    "login": ("exige login", "aviso"),
    "sem-site": ("", "ok"),   # já existe o selo "sem site" na aba
    "ok": ("", "ok"),
}


def _curl():
    try:
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome")
    except Exception:
        return None


def probe(info):
    """Devolve (status, detalhe). status ∈ ok/mudou/bloqueado/morto/login/sem-site."""
    site = info.site_publico
    if not site:
        return "sem-site", ""

    dominio = re.sub(r"^https?://", "", site).split("/")[0]

    scr = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False})
    scr.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

    def classifica(r):
        final = re.sub(r"^https?://", "", r.url).split("/")[0]
        corpo = r.text[:600].lower()
        if r.status_code in (401, 403) and ("login" in corpo or "sign in" in corpo):
            return "login", f"{r.status_code} exige login"
        if "just a moment" in corpo or "cf-challenge" in corpo or "cf_chl" in corpo:
            return "bloqueado", "Cloudflare challenge"
        if any(x in final for x in ("get-entry", "verificator", "parking", "sedoparking")):
            return "morto", f"redireciona pra {final}"
        base = ".".join(dominio.split(".")[-2:])
        base_final = ".".join(final.split(".")[-2:])
        if base_final and base_final != base and "www." not in final:
            return "mudou", f"-> {final}"
        if r.status_code == 200:
            return "ok", final
        if r.status_code == 404:
            return "morto", "404"
        if r.status_code >= 500:
            return "morto", f"HTTP {r.status_code}"
        return "ok", f"{r.status_code} {final}"

    try:
        r = scr.get(site, timeout=25, allow_redirects=True)
        st, det = classifica(r)
        if st != "bloqueado":
            return st, det
    except Exception as e:
        primeiro_erro = f"{type(e).__name__}"
    else:
        primeiro_erro = "cf"

    cc = _curl()
    if cc is not None:
        try:
            r = cc.get(site, timeout=25, headers={"User-Agent": UA})
            return classifica(r)
        except Exception as e:
            return "morto", f"{primeiro_erro}/{type(e).__name__}"
    return "morto", primeiro_erro


def carregar_cache():
    """{nome_provider: {"status": ..., "detalhe": ..., "quando": epoch}} ou {}."""
    try:
        return json.loads(ARQUIVO_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salvar_cache(resultados):
    ARQUIVO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_CACHE.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")


def sondar_todos(providers, progresso=None, max_workers=12):
    """Sonda a lista e devolve o dict do cache (também salvando em disco).

    progresso: callback opcional (atual, total, nome).
    """
    from concurrent.futures import ThreadPoolExecutor

    resultados = {}
    total = len(providers)
    feitos = [0]

    def um(info):
        st, det = probe(info)
        feitos[0] += 1
        if progresso:
            progresso(feitos[0], total, info.nome)
        return info.nome, st, det

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for nome, st, det in ex.map(um, providers):
            resultados[nome] = {"status": st, "detalhe": det, "quando": time.time()}

    salvar_cache(resultados)
    return resultados
