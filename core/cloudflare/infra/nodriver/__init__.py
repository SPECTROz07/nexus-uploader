import base64
import random
import tldextract
import nodriver as uc
from time import sleep
from bs4 import BeautifulSoup

from core.cloudflare.domain.request_entity import Request
from core.cloudflare.domain.bypass_repository import BypassRepository
from core.cloudflare.infra.nodriver.chrome import find_chrome_executable
from core.session_manager import get_request, delete_request, insert_request, SessionData as RequestData



class Cloudflare(BypassRepository):
    def is_cloudflare_blocking(self, html: str) -> bool:
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string if soup.title else ""
        return "Just a moment..." in title or "Um momento…" in title or "Checking your browser" in title

    async def _start_browser(self, background: bool):
        # Para evitar problemas com modo headless e Cloudflare, usar modo não headless
        return await uc.start(browser_executable_path=find_chrome_executable(), headless=background)

    async def _get_content_task(self, url: str, background: bool):
        """Tarefa assíncrona para obter o conteúdo de uma página."""
        async with await self._start_browser(background) as browser:
            page = await browser.get(url)
            
            # Wait for a total of 60 seconds, checking every 5 seconds.
            total_wait_time = 60
            interval = 5
            for _ in range(total_wait_time // interval):
                await page.sleep(interval)
                current_content = await page.get_content()
                if not self.is_cloudflare_blocking(current_content):
                    print("Desafio Cloudflare resolvido.")
                    # Save session on success
                    extract = tldextract.extract(url)
                    onlydomain = f"{extract.domain}.{extract.suffix}"
                    if get_request(onlydomain) is None:
                        agent = await page.evaluate('navigator.userAgent')
                        headers = {'user-agent': agent}
                        cookies_from_browser = await browser.cookies.get_all()
                        cookies = {c.name: c.value for c in cookies_from_browser}
                        if 'cf_clearance' in cookies:
                            insert_request(RequestData(domain=onlydomain, headers=headers, cookies=cookies))
                    return current_content

                else:
                    print("Aguardando resolução do Cloudflare...")
                    # Simulate some interaction
                    try:
                        await page.evaluate(f"""
                            (() => {{
                                window.scrollTo(0, Math.random() * document.body.scrollHeight);
                            }})()
                        """)
                    except Exception:
                        pass # Ignore interaction errors

            # If the loop finishes, return the content anyway.
            print("Tempo de espera esgotado, retornando conteúdo atual.")
            return await page.get_content()

    def bypass_cloudflare_no_capcha(self, url: str, background: bool = True) -> str:
        return uc.loop().run_until_complete(self._get_content_task(url, background))

    def is_cloudflare_time_out(self, html: str) -> bool:
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string if soup.title else ""
        return "Gateway time-out" in title

    def is_cloudflare_bad_gatway(self, html: str) -> bool:
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string if soup.title else ""
        return "Bad gateway" in title

    def is_cloudflare_attention(self, html: str) -> bool:
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string if soup.title else ""
        return "Attention Required! | Cloudflare" in title

    def is_cloudflare_enable_cookies(self, html: str) -> bool:
        soup = BeautifulSoup(html, 'html.parser')
        return bool(soup.select_one('div#cookie-alert'))

    # ... (o resto do arquivo, incluindo _fetch_task e as funcoes sincronas, pode permanecer o mesmo)
    async def _fetch_task(self, domain: str, url: str, background: bool, method: str = "GET"):
        """Tarefa assíncrona para buscar dados com fetch."""
        async with await self._start_browser(background) as browser:
            page = await browser.get(domain)
            
            for _ in range(10):
                if not self.is_cloudflare_blocking(await page.get_content()):
                    break
                sleep(1.5)
            else:
                raise Exception("Desafio do Cloudflare nao foi resolvido para a sessao de fetch.")
            
            # Injetar cookies salvos se existirem, para garantir a sessao
            extract = tldextract.extract(domain)
            onlydomain = f"{extract.domain}.{extract.suffix}"
            request_data = get_request(onlydomain)
            if request_data and request_data.cookies:
                cf_cookie_value = request_data.cookies.get('cf_clearance')
                if cf_cookie_value:
                    js_code = f'document.cookie = "cf_clearance={cf_cookie_value}; path=/; max-age=3600; secure; samesite=strict";'
                    await page.evaluate(js_code)

            fetch_options = f'{{method: "{method.upper()}"}}' if method.upper() != "GET" else ""
            fetch_script = f'''
                fetch("{url}", {fetch_options})
                .then(response => response.arrayBuffer())
                .then(buffer => btoa(String.fromCharCode(...new Uint8Array(buffer))));
            '''
            b64_content = await page.evaluate(fetch_script, await_promise=True)
            return base64.b64decode(b64_content) if b64_content else None

    def bypass_cloudflare_no_capcha_fetch(self, domain: str, url: str, background: bool = False) -> any:
        return uc.loop().run_until_complete(self._fetch_task(domain, url, background, "GET"))

    def bypass_cloudflare_no_capcha_post(self, domain: str, url: str, background: bool = False) -> any:
        return uc.loop().run_until_complete(self._fetch_task(domain, url, background, "POST"))

    def bypass_cloudflare(self, url: str) -> Request:
        headers = {}
        cookies = {}
        async def get_cloudflare_cookie():
            nonlocal headers, cookies
            async with await self._start_browser(background=False) as browser:
                page = await browser.get(url)
                agent = await page.evaluate('navigator.userAgent')
                headers = {'user-agent': agent}
                while True:
                    page_content = await page.get_content()
                    if self.is_cloudflare_blocking(page_content):
                        sleep(1)
                    else:
                        break
                cookies_from_browser = await browser.cookies.get_all()
                for cookie in cookies_from_browser:
                    if cookie.name == 'cf_clearance':
                        cookies = {'cf_clearance': cookie.value}
        uc.loop().run_until_complete(get_cloudflare_cookie())
        return Request(user_agent=headers, cloudflare_cookie_value=cookies)