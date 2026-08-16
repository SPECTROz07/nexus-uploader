import json
import logging

import cloudscraper

from core.config import API_BASE_URL, APP_KEY

logger = logging.getLogger(__name__)


class ApiClient:
    """
    Cliente da API do Nexus (login por usuário/senha).

    O que mudou desde a última versão que funcionava:
      - O host é o .com (o .site é o SPA e devolvia HTML → "Expecting value").
      - O login é POST /api/login (o /api/token/ antigo saiu de uso).
      - Toda request leva X-App-Key (passa o OriginGuard do backend) e
        X-Skip-Encrypt (o backend cifra respostas por padrão; isso pede JSON
        puro). O JWT vai em Authorization: Bearer e, por ele, o CSRF é dispensado.
    """

    def __init__(self):
        self.session = cloudscraper.create_scraper()
        self.access_token = None
        self.username = None
        self.password = None
        self.user = None

    # ------------------------------------------------------------------
    # HEADERS
    # ------------------------------------------------------------------

    def _base_headers(self):
        return {
            "X-App-Key": APP_KEY,
            "X-Skip-Encrypt": "true",
            "Accept": "application/json",
            "User-Agent": "NexusUploader/2.0",
        }

    def _headers(self, for_json=True):
        h = self._base_headers()
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        if for_json:
            h["Content-Type"] = "application/json"
        return h

    # ------------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------------

    def login(self, username, password):
        try:
            r = self.session.post(
                f"{API_BASE_URL}/api/login",
                json={"username": username, "password": password},
                headers=self._base_headers(),
                timeout=25,
            )
        except Exception as e:
            return False, f"Erro de conexão: {e}"

        if r.status_code == 200:
            try:
                data = r.json()
            except json.JSONDecodeError:
                return False, "Resposta inesperada do servidor (não era JSON)."
            self.access_token = data.get("token")
            self.user = data.get("user")
            self.username = username
            self.password = password
            if not self.access_token:
                return False, "Login sem token na resposta."
            return True, data

        try:
            body = r.json()
            msg = body.get("error") or body.get("message") or r.text[:160]
        except json.JSONDecodeError:
            msg = r.text[:160]
        return False, f"Erro {r.status_code}: {msg}"

    def get_current_user_details(self):
        return self._req("get", "/api/me")

    def _relogin(self):
        if self.username and self.password:
            return self.login(self.username, self.password)[0]
        return False

    # ------------------------------------------------------------------
    # HTTP genérico
    # ------------------------------------------------------------------

    def _req(self, method, endpoint, is_retry=False, **kwargs):
        url = endpoint if endpoint.startswith("http") else f"{API_BASE_URL}{endpoint}"
        is_json = "json" in kwargs
        try:
            kwargs.setdefault("timeout", 60)
            r = self.session.request(method, url, headers=self._headers(for_json=is_json), **kwargs)
            r.raise_for_status()
            if r.text:
                try:
                    return True, r.json()
                except json.JSONDecodeError:
                    return False, "Resposta inesperada do servidor (não era JSON)."
            return True, None
        except cloudscraper.requests.exceptions.HTTPError as e:
            # 401 → tenta relogar uma vez (o JWT pode ter expirado)
            if e.response.status_code == 401 and not is_retry and self._relogin():
                return self._req(method, endpoint, is_retry=True, **kwargs)
            try:
                detail = e.response.json()
                detail = detail.get("error") or detail
            except json.JSONDecodeError:
                detail = e.response.text
            return False, f"Erro API ({e.response.status_code}): {detail}"
        except cloudscraper.requests.exceptions.RequestException as e:
            return False, f"Erro de Conexão: {e}"

    # ------------------------------------------------------------------
    # OBRAS / CATEGORIAS (leitura)
    # ------------------------------------------------------------------

    @staticmethod
    def _normaliza_obra(m: dict) -> dict:
        return {
            "id": m.get("id"),
            "title": m.get("title", ""),
            "slug": m.get("slug"),
            "cover_url": m.get("coverImage"),
            "status": m.get("status"),
        }

    def get_mangas(self, q="", **kwargs):
        params = {"limit": 50}
        if q:
            params["search"] = q
        ok, data = self._req("get", "/api/mangas", params=params, **kwargs)
        if not ok:
            return ok, data
        itens = data.get("data", data.get("results", [])) if isinstance(data, dict) else []
        return True, {
            "results": [self._normaliza_obra(m) for m in itens],
            "count": data.get("total", len(itens)) if isinstance(data, dict) else len(itens),
            "next": None,
        }

    def get_all_mangas_paginated(self):
        todas = []
        pagina = 1
        while True:
            ok, data = self._req("get", "/api/mangas", params={"page": pagina, "limit": 100})
            if not ok:
                return (False, todas if todas else data)
            itens = data.get("data", []) if isinstance(data, dict) else []
            todas.extend(self._normaliza_obra(m) for m in itens)
            total_paginas = data.get("pages", 1) if isinstance(data, dict) else 1
            if pagina >= total_paginas or not itens:
                break
            pagina += 1
        return True, todas

    def get_all_categories_paginated(self):
        ok, data = self._req("get", "/api/categories")
        if not ok:
            return ok, data
        if isinstance(data, list):
            arr = data
        else:
            arr = data.get("results", data.get("data", []))
        return True, [{"id": c.get("id"), "name": c.get("name")} for c in arr]

    def get_manga_details(self, manga_id, **kwargs):
        ok, data = self._req("get", f"/api/manga/id/{manga_id}", **kwargs)
        if not ok:
            return ok, data
        # Formato novo: {chapterCount, chapterIds:[{ID,Number}], manga:{...}}.
        # Achata pra um dict com 'title' no topo e 'chapters' normalizado, que é
        # o que o cache da GUI e o resto do app esperam.
        if isinstance(data, dict) and "manga" in data:
            norm = dict(data.get("manga") or {})
            norm["chapters"] = [
                {"id": c.get("ID", c.get("id")), "number": c.get("Number", c.get("number"))}
                for c in (data.get("chapterIds") or [])
            ]
            norm["chapterCount"] = data.get("chapterCount", len(norm["chapters"]))
            return True, norm
        return True, data

    @staticmethod
    def _chapters_do_detalhe(data):
        # O endpoint /api/manga/id/<id> passou a devolver os capítulos em
        # "chapterIds" (lista de {ID, Number}); o formato antigo usava "chapters".
        # Cobrimos os dois (top-level e dentro de "data") pra não quebrar de novo.
        if not isinstance(data, dict):
            return []
        for fonte in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
            if not isinstance(fonte, dict):
                continue
            chs = fonte.get("chapters") or fonte.get("chapterIds")
            if chs:
                return chs
        return []

    def get_chapters(self, manga_id: int):
        ok, data = self._req("get", f"/api/manga/id/{manga_id}")
        if not ok:
            return ok, data
        out = []
        for c in self._chapters_do_detalhe(data):
            # formato novo usa ID/Number (maiúsculos); o antigo id/number
            numero = c.get("number", c.get("Number"))
            out.append({
                "id": c.get("id", c.get("ID")),
                "number": numero,
                "chapter_number": numero,
                "title": c.get("title", c.get("Title")),
            })
        return True, out

    def get_chapter_numbers_list(self, manga_id: int):
        ok, chapters = self.get_chapters(manga_id)
        if not ok:
            return ok, chapters
        return True, [c["number"] for c in chapters if c.get("number") is not None]

    def get_chapter_details(self, chapter_id):
        return self._req("get", f"/api/chapter/{chapter_id}")

    # ------------------------------------------------------------------
    # ESCRITA
    # ------------------------------------------------------------------

    def create_or_get_chapter(self, manga_id, chapter_config):
        payload = {
            "chapter_number": str(chapter_config.get("chapter_number", "")),
            "title": chapter_config.get("title", "") or "",
            "access_level": chapter_config.get("access_level", "public") or "public",
        }
        if chapter_config.get("vip_duration_hours") is not None:
            payload["vip_duration_hours"] = chapter_config["vip_duration_hours"]
        if chapter_config.get("coin_cost") is not None:
            payload["coin_cost"] = chapter_config["coin_cost"]
        return self._req("post", f"/api/manga/{manga_id}/create_or_get_chapter", json=payload)

    def update_chapter_with_pages(self, chapter_id, image_urls):
        return self._req("post", f"/api/chapter/{chapter_id}/update_page_paths",
                         json={"pages": image_urls})

    def delete_chapter(self, chapter_id):
        return self._req("delete", f"/api/chapter/{chapter_id}")

    def create_manga(self, manga_data: dict):
        return self._req("post", "/api/manga", json=manga_data)

    def update_manga(self, manga_id: int, manga_data: dict):
        return self._req("put", f"/api/manga/{manga_id}", json=manga_data)

    def create_category(self, category_name: str):
        return self._req("post", "/api/categories", json={"name": category_name})
