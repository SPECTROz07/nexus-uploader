# --- START OF FILE run_bot.py ---

import sys
import os
import multiprocessing
from pathlib import Path
from functools import partial

if __name__ == "__main__" and getattr(sys, 'frozen', False):
    multiprocessing.freeze_support()

if getattr(sys, 'frozen', False):
    base_path = Path(sys.executable).parent
else:
    base_path = Path(__file__).parent

if base_path.exists() and str(base_path) not in sys.path:
    sys.path.insert(0, str(base_path))

if getattr(sys, 'frozen', False):
    playwright_browsers_path = base_path / 'playwright_browsers'
    if playwright_browsers_path.exists():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(playwright_browsers_path)


import json
import time
import logging
import argparse
import shutil
import importlib
import re
import secrets
from datetime import datetime, timedelta

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, PatternMatchingEventHandler
from PySide6.QtCore import QCoreApplication, QThreadPool, QObject, QTimer
from PySide6.QtNetwork import QLocalSocket

from core.api_client import ApiClient
from core.credentials import CredentialsManager
from core.s3_handler import ContaboS3Handler
from core.processing import natural_sort_key, extract_chapter_number_py
from core.settings_manager import SettingsManager
from providers.base import BaseProvider

def _norm_num(v):
    """Normaliza número de capítulo pra comparação/gravação: '1.0'/1.0 -> '1',
    mantém fracionários ('1.5') e não-numéricos como estão. Providers devolvem
    ora string ora float; sem isto, '1.0' != '1' e o cap vira duplicata."""
    s = str(v).strip()
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (ValueError, TypeError):
        pass
    return s


def setup_logging(bot_id):
    bot_id_upper = bot_id.upper()
    logging.basicConfig(level=logging.INFO,
                        format=f'%(asctime)s - %(levelname)s - [{bot_id_upper}] - %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])

def send_status_update(message, bot_id):
    socket = QLocalSocket()
    socket_name = "NexusUploaderSocket"
    
    try:
        message_with_id = message.copy()
        if 'data' not in message_with_id: message_with_id['data'] = {}
        message_with_id['data']['bot_id'] = bot_id
        payload = json.dumps(message_with_id).encode('utf-8')

        socket.connectToServer(socket_name)
        
        if not socket.waitForConnected(1000):
            logging.error(f"[Bot IPC] Falha ao conectar ao socket da GUI ('{socket_name}'): {socket.errorString()}")
            logging.error("[Bot IPC] Verifique se a interface gráfica principal está aberta e funcionando.")
            return

        bytes_written = socket.write(payload)
        if bytes_written == -1:
            logging.error(f"[Bot IPC] Erro ao escrever no socket: {socket.errorString()}")
            return

        if not socket.waitForBytesWritten(2000):
             logging.error(f"[Bot IPC] Timeout ao enviar dados para a GUI: {socket.errorString()}")

    except Exception as e:
        logging.error(f"[Bot IPC] Erro inesperado ao preparar ou enviar status: {e}")
    finally:
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.UnconnectedState:
            socket.waitForDisconnected(500)

class FolderChangeHandler(FileSystemEventHandler):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
    def on_any_event(self, event):
        path_to_register = Path(event.src_path).parent if not event.is_directory else Path(event.src_path)
        self.manager.add_to_activity_queue(path_to_register)

class ConfigChangeHandler(PatternMatchingEventHandler):
    def __init__(self, manager):
        super().__init__(patterns=["monitoramento_bots.json"])
        self.manager = manager
    def on_modified(self, event):
        self.manager.logger.info("Arquivo 'monitoramento_bots.json' modificado. Recarregando...")
        self.manager.schedule_reload()

class BotManager(QObject):
    def __init__(self, bot_id, config_path="monitoramento_bots.json"):
        super().__init__()
        self.bot_id = bot_id
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_path = Path(config_path)
        
        self.monitoramento_config = {}
        self.provider_configs = []
        
        self.activity_queue = {}
        self.currently_processing = set()
        
        self.settings_manager = SettingsManager()
        self.credentials_manager = CredentialsManager()
        s3_config_path = self.settings_manager.get("s3_config_path")
        self.s3_handler = ContaboS3Handler(config_path=s3_config_path)
        
        self.api_client = ApiClient()
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(2)
        self.online_chapters_cache = {}
        self.cache_update_timestamps = {}
        self.manga_details_cache = {}
        self.manga_observer = Observer()
        self.config_observer = Observer()
        self._reload_scheduled = False

        self.providers = {}
        self.last_provider_check = 0
        self.last_force_check_timestamp = 0

        self.main_timer = QTimer(self)
        self.main_timer.timeout.connect(self.run_cycle)

    def load_config(self):
        self.logger.info("Carregando configuração...")
        if not self.config_path.exists():
            self.logger.warning(f"Arquivo de configuração '{self.config_path}' não encontrado.")
            return False
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                full_config = json.load(f)
            
            bot_id_found = None
            for key in full_config:
                if key.lower() == self.bot_id.lower():
                    bot_id_found = key
                    break
            
            if not bot_id_found:
                self.logger.error(f"ID '{self.bot_id}' não encontrado na config.")
                self.monitoramento_config = {}
                self.provider_configs = []
                return True

            bot_specific_config = full_config[bot_id_found]
            
            self.monitoramento_config = {}
            self.provider_configs = []

            for key, value in bot_specific_config.items():
                if key.startswith('_'):
                    continue
                
                if 'provider' in value and 'manga_url' in value:
                    value['_config_key'] = key
                    self.provider_configs.append(value)
                else:
                    self.monitoramento_config[Path(key)] = value

            self.logger.info(f"Configuração carregada para '{self.bot_id}':")
            self.logger.info(f" - {len(self.monitoramento_config)} obra(s) monitoradas por pasta.")
            self.logger.info(f" - {len(self.provider_configs)} obra(s) monitoradas por provider.")
            return True
        except Exception as e:
            self.logger.error(f"Erro ao carregar configuração: {e}", exc_info=True)
            return False

    def login(self):
        self.logger.info("Realizando login..."); username, password = self.credentials_manager.load_credentials()
        if not (username and password): self.logger.error("Credenciais não encontradas. Faça login no app primeiro."); return False
        success, _ = self.api_client.login(username, password)
        if success: self.logger.info(f"Login como '{username}' bem-sucedido.")
        else: self.logger.error("Falha no login.")
        return success

    def sync_online_chapters(self):
        self.logger.info("Sincronizando cache de capítulos online...")
        manga_ids_from_folders = {conf['manga_id'] for conf in self.monitoramento_config.values() if 'manga_id' in conf}
        manga_ids_from_providers = {conf['manga_id'] for conf in self.provider_configs if 'manga_id' in conf}
        all_manga_ids = manga_ids_from_folders.union(manga_ids_from_providers)

        for manga_id in all_manga_ids:
            success, chapters_data = self.api_client.get_chapter_numbers_list(manga_id)
            if success and isinstance(chapters_data, list):
                self.online_chapters_cache[str(manga_id)] = set(map(str, chapters_data))
                self.cache_update_timestamps[str(manga_id)] = time.time()
                self.logger.info(f"  - Obra ID {manga_id}: {len(chapters_data)} capítulos cacheados.")
            else:
                self.logger.warning(f"  - Falha ao cachear capítulos para Obra ID {manga_id}: {chapters_data}")

    def scan_all_folders_on_startup(self):
        if not self.monitoramento_config: return
        self.logger.info("--- INICIANDO ESCANEAMENTO DE PASTAS EXISTENTES ---")
        all_chapter_paths = [p for mf in self.monitoramento_config.keys() if mf.exists() for p in mf.iterdir() if p.is_dir()]
        for i, chapter_path in enumerate(all_chapter_paths):
            self.logger.info(f"Verificando pasta {i+1}/{len(all_chapter_paths)}: {chapter_path.name}")
            self.process_folder_if_needed(chapter_path)
        self.logger.info("--- ESCANEAMENTO DE PASTAS FINALIZADO ---")

    def add_to_activity_queue(self, folder_path: Path):
        is_subfolder = any(folder_path.is_relative_to(p) and folder_path != p for p in self.monitoramento_config.keys())
        if is_subfolder: self.activity_queue[folder_path] = time.time()

    def check_activity_queue(self):
        now = time.time()
        for folder_path, last_activity_time in list(self.activity_queue.items()):
            manga_id, wait_time = self.get_manga_config_for_path(folder_path)
            if manga_id and (now - last_activity_time) > wait_time:
                self.logger.info(f"Pasta '{folder_path.name}' estabilizou. Processando...")
                self.process_folder_if_needed(folder_path)
                if folder_path in self.activity_queue: del self.activity_queue[folder_path]

    def get_manga_config_for_path(self, folder_path: Path):
        for monitored_folder, config in self.monitoramento_config.items():
            if folder_path.is_relative_to(monitored_folder):
                return config.get('manga_id'), config.get('wait_time_seconds', 180)
        return None, 180

    def process_folder_if_needed(self, folder_path: Path):
        folder_str = str(folder_path)
        if folder_str in self.currently_processing: return
        manga_id, _ = self.get_manga_config_for_path(folder_path)
        if not manga_id: return
        
        chapter_number = extract_chapter_number_py(folder_path.name)
        if chapter_number is None: return
        
        online_chaps = self.online_chapters_cache.get(str(manga_id), set())
        if chapter_number in online_chaps:
            return
        
        self.currently_processing.add(folder_str)
        self.logger.info(f"  - PENDENTE (Pasta): '{folder_path.name}'")
        
        image_paths = [str(f) for f in folder_path.glob('*') if f.is_file()]
        if not image_paths:
            self.logger.warning(f"Pasta '{folder_path.name}' detectada, mas está vazia. Ignorando.")
            self.currently_processing.discard(folder_str)
            return

        config = {
            'path': str(folder_path), 
            'images': image_paths, 
            'number': chapter_number
        }
        self.request_upload_from_gui(config, manga_id, from_provider=False)

    def load_provider(self, provider_name: str) -> BaseProvider | None:
        if provider_name in self.providers:
            return self.providers[provider_name]
        try:
            module = importlib.import_module(f"providers.{provider_name}_provider")
            class_name_parts = [part.capitalize() for part in provider_name.split('_')]
            class_name = "".join(class_name_parts) + "Provider"
            ProviderClass = getattr(module, class_name, None)
            if ProviderClass is None:
                # Nome fora da convencao (ex.: ArthurScanProvider vs
                # ArthurscanProvider): usa a classe Provider definida no proprio
                # arquivo, ignorando bases e classes importadas de outros modulos.
                candidatas = [obj for attr in dir(module)
                              if attr.endswith("Provider")
                              and attr not in ("BaseProvider", "ScanApiProvider")
                              for obj in [getattr(module, attr)]
                              if isinstance(obj, type) and obj.__module__ == module.__name__]
                if not candidatas:
                    raise AttributeError(f"nenhuma classe *Provider definida em providers.{provider_name}_provider")
                ProviderClass = candidatas[0]
            instance = ProviderClass()
            self.providers[provider_name] = instance
            self.logger.info(f"Provider '{provider_name}' carregado com sucesso (Classe: {ProviderClass.__name__}).")
            return instance
        except (ImportError, AttributeError) as e:
            self.logger.error(f"Falha ao carregar provider '{provider_name}': {e}. Verifique o nome do arquivo e da classe.", exc_info=True)
            return None

    def check_providers_periodically(self):
        if not self.provider_configs:
            return

        is_forced_run = False
        alvo_manga_ids = None
        force_check_signal = base_path / "force_check.signal"

        if force_check_signal.exists():
            try:
                content = force_check_signal.read_text().strip()
                if content:
                    # Formato: "<timestamp>" (geral) ou "<timestamp>|id1,id2" (só
                    # as obras listadas). O separador mantém compatibilidade com
                    # o sinal antigo, que era só o float.
                    partes = content.split('|', 1)
                    timestamp_in_file = float(partes[0])
                    if timestamp_in_file > self.last_force_check_timestamp:
                        if len(partes) > 1 and partes[1].strip():
                            alvo_manga_ids = {s.strip() for s in partes[1].split(',') if s.strip()}
                            self.logger.info(f"!!! SINAL DE VERIFICAÇÃO FORÇADA (obras: {', '.join(sorted(alvo_manga_ids))}) !!!")
                        else:
                            self.logger.info("!!! SINAL DE VERIFICAÇÃO FORÇADA DA GERÊNCIA DETECTADO !!!")
                        is_forced_run = True
                        self.last_force_check_timestamp = timestamp_in_file
            except (ValueError, OSError) as e:
                self.logger.error(f"Erro ao ler o arquivo de sinal: {e}")

        if not is_forced_run:
            now = time.time()
            cooldown = 5 * 60
            if (now - self.last_provider_check) < cooldown:
                return

        self.last_provider_check = time.time()

        self.logger.info("--- INICIANDO VERIFICAÇÃO DE PROVIDERS ---")
        self.run_provider_checks(is_forced_run=is_forced_run, alvo_manga_ids=alvo_manga_ids)
        self.logger.info("--- VERIFICAÇÃO DE PROVIDERS FINALIZADA ---")

    def _calculate_next_check_time(self, config):
        now = datetime.now()
        mode = config.get('schedule_mode', 'interval')

        if mode == 'weekly':
            days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
            scheduled_days = [days_map[day] for day in config.get('schedule_days', []) if day in days_map]
            if not scheduled_days:
                return now + timedelta(hours=config.get('check_interval_hours', 6))

            schedule_time_str = config.get('schedule_time', '18:00')
            schedule_time_obj = datetime.strptime(schedule_time_str, "%H:%M").time()
            
            today_weekday = now.weekday()
            
            for i in range(8):
                next_day_offset = (today_weekday + i) % 7
                if next_day_offset in scheduled_days:
                    if i == 0 and now.time() >= schedule_time_obj:
                        continue
                    
                    next_date = (now + timedelta(days=i)).date()
                    return datetime.combine(next_date, schedule_time_obj)
            return now + timedelta(days=7)
        else:
            interval_hours = config.get('check_interval_hours', 6)
            return now + timedelta(hours=interval_hours)
    
    def _sanitize_foldername(self, name: str) -> str:
        name = name.lower()
        name = re.sub(r'https?://[^/]+/', '', name)
        name = re.sub(r'[\\/*?:"<>| \']', '_', name)
        name = re.sub(r'__+', '_', name)
        return name.strip('_/')

    def run_provider_checks(self, is_forced_run=False, alvo_manga_ids=None):
        config_changed = False

        for config in self.provider_configs:
            if alvo_manga_ids is not None and str(config.get("manga_id")) not in alvo_manga_ids:
                continue
            if not is_forced_run:
                now = datetime.now()
                next_check_str = config.get("next_check_at")
                if next_check_str:
                    try:
                        next_check_time = datetime.fromisoformat(next_check_str)
                        if now < next_check_time:
                            self.logger.debug(f"Verificação para '{config.get('manga_url')}' agendada para {next_check_time.strftime('%d/%m %H:%M')}. Pulando.")
                            continue
                    except ValueError:
                        self.logger.warning(f"Formato de data inválido para 'next_check_at' em '{config.get('manga_url')}'. Verificando agora.")

            self.logger.info(f"Executando verificação ({'Forçada' if is_forced_run else 'Agendada'}) para: {config.get('manga_url')}")
            
            provider_name = config.get("provider")
            manga_url = config.get("manga_url")
            manga_id = config.get("manga_id")

            try:
                provider = self.load_provider(provider_name)
                if not provider: continue

                source_chapters = provider.get_chapters(manga_url)
                if not source_chapters:
                    self.logger.info(f"Nenhum capítulo encontrado no provider '{provider_name}' para {manga_url}.")
                    continue
                
                success, existing_chapters_list = self.api_client.get_chapter_numbers_list(manga_id)
                existing_chapter_numbers = set()
                if success and isinstance(existing_chapters_list, list):
                    existing_chapter_numbers = set(map(_norm_num, existing_chapters_list))

                # normaliza os DOIS lados (str sem ".0"): providers devolvem número
                # ora string ("1"), ora float (1.0); sem isto "1.0" != "1" e o cap
                # existente parece novo -> re-upload / duplicata "1.0" no site
                new_chapters_to_upload = [chap for chap in source_chapters
                                          if _norm_num(chap['number']) not in existing_chapter_numbers]

                if not new_chapters_to_upload:
                    self.logger.info(f"Nenhum capítulo novo/faltante encontrado para a obra ID {manga_id}.")
                    continue

                self.logger.info(f"Encontrados {len(new_chapters_to_upload)} capítulos novos/faltantes para a obra ID {manga_id}. Iniciando download...")
                
                sorted_new_chapters = sorted(new_chapters_to_upload, key=lambda c: natural_sort_key(c['number']))
                
                base_download_dir = base_path / "temp_providers"
                provider_dir = base_download_dir / self._sanitize_foldername(provider_name)
                manga_slug = self._sanitize_foldername(manga_url.split('/manga/')[-1] if '/manga/' in manga_url else manga_url.split('/')[-1])
                manga_download_dir = provider_dir / manga_slug
                manga_download_dir.mkdir(parents=True, exist_ok=True)

                for chap_to_upload in sorted_new_chapters:
                    temp_dir_for_chapter = manga_download_dir / f"manga_{manga_id}_ch_{chap_to_upload['number']}_{secrets.token_hex(4)}"
                    temp_dir_for_chapter.mkdir()
                    
                    self.logger.info(f"Baixando capítulo {chap_to_upload['number']} para: {temp_dir_for_chapter}")
                    image_paths = provider.download_chapter(chap_to_upload['url'], str(temp_dir_for_chapter))
                    
                    if not image_paths:
                        self.logger.warning(f"Download do capítulo {chap_to_upload['number']} não retornou imagens. Pulando upload.")
                        shutil.rmtree(temp_dir_for_chapter)
                        continue
                        
                    upload_config = {
                        'path': str(temp_dir_for_chapter),
                        'images': image_paths,
                        # número normalizado: cria "1" no site, nunca "1.0"
                        'number': _norm_num(chap_to_upload['number'])
                    }
                    self.request_upload_from_gui(upload_config, manga_id, from_provider=True, temp_dir_to_clean=str(temp_dir_for_chapter))
                    config["last_new_chapter"] = str(chap_to_upload['number'])
                    config["last_new_chapter_at"] = datetime.now().isoformat()

            finally:
                config["last_check_at"] = datetime.now().isoformat()
                next_run_time = self._calculate_next_check_time(config)
                config["next_check_at"] = next_run_time.isoformat()
                config_changed = True

        if config_changed:
            self.logger.info("Salvando datas de agendamento atualizadas no arquivo de configuração...")
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    full_config = json.load(f)
                
                bot_id_found = None
                for key in full_config:
                    if key.lower() == self.bot_id.lower():
                        bot_id_found = key
                        break
                if bot_id_found:
                    bot_config = full_config[bot_id_found]
                    for provider_conf in self.provider_configs:
                        key = provider_conf.get('_config_key')
                        if key and key in bot_config:
                            bot_config[key] = {k: v for k, v in provider_conf.items() if k != '_config_key'}
                    full_config[bot_id_found] = bot_config
                    with open(self.config_path, 'w', encoding='utf-8') as f:
                        json.dump(full_config, f, indent=4)
            except Exception as e:
                self.logger.error(f"Falha ao salvar o arquivo de configuração: {e}")

    def request_upload_from_gui(self, chapter_config, manga_id, from_provider=False, temp_dir_to_clean=None):
        task_id = f"auto-{os.path.basename(chapter_config['path'])}-{time.time()}"
        
        gui_task_config = {
            'task_id': task_id,
            'path': chapter_config['path'],
            'images': chapter_config['images'],
            'number': str(chapter_config.get('number')),
            'manga_id': manga_id,
            'temp_dir_to_clean': temp_dir_to_clean
        }
        
        self.logger.info(f"Enviando solicitação de upload para a GUI para o capítulo: {gui_task_config['number']}")
        
        send_status_update({
            "event": "enqueue_upload_task",
            "data": {
                "manga_id": manga_id,
                "chapter_config": gui_task_config
            }
        }, self.bot_id)

        if manga_id and gui_task_config['number']:
            manga_id_str = str(manga_id)
            if manga_id_str not in self.online_chapters_cache:
                self.online_chapters_cache[manga_id_str] = set()
            self.online_chapters_cache[manga_id_str].add(gui_task_config['number'])
            self.logger.info(f"Cache interno do bot atualizado para o capítulo '{gui_task_config['number']}'.")
        
        if not from_provider:
            self.currently_processing.discard(chapter_config['path'])

    def start(self):
        if not self.load_config() or not self.login(): 
            QCoreApplication.instance().quit()
            return
            
        self.sync_online_chapters()
        self.scan_all_folders_on_startup()
        self.start_observers()
        self.logger.info("Worker ativo e em modo de espera por novas atividades.")
        
        self.main_timer.start(5000)

    def run_cycle(self):
        if self._reload_scheduled: 
            self.reload_monitoring_config()
        
        self.check_activity_queue()
        self.check_providers_periodically()

    def reload_monitoring_config(self):
        self.logger.info("Recarregando configuração...")
        self.stop_observers()
        self.activity_queue.clear(); self.currently_processing.clear(); self.providers.clear()
        self.load_config(); self.sync_online_chapters(); self.scan_all_folders_on_startup(); self.start_observers()
        self._reload_scheduled = False

    def start_observers(self):
        self.stop_observers()
        self.manga_observer = Observer()
        event_handler = FolderChangeHandler(self)
        for folder_path in self.monitoramento_config.keys():
            if folder_path.exists():
                self.manga_observer.schedule(event_handler, str(folder_path), recursive=True)
                self.logger.info(f"Monitorando novas pastas/arquivos em: {folder_path}")
        self.manga_observer.start()
        config_path_dir = self.config_path.parent.resolve()
        self.config_observer = Observer()
        self.config_observer.schedule(ConfigChangeHandler(self), str(config_path_dir))
        self.config_observer.start()

    def stop_observers(self):
        if self.manga_observer.is_alive(): self.manga_observer.stop(); self.manga_observer.join()
        if self.config_observer.is_alive(): self.config_observer.stop(); self.config_observer.join()

    def schedule_reload(self):
        self._reload_scheduled = True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Worker Bot para Monitoramento de Mangás.")
    parser.add_argument('--bot-id', type=str, required=True, help='O ID único do bot (ex: bot_1)')
    args = parser.parse_args()
    
    setup_logging(args.bot_id)
    
    logging.info(f"Worker '{args.bot_id}' inicializado. Aguardando comandos da gerência (Kaoryn).")
    
    try:
        from playwright.sync_api import sync_playwright
        logging.info("Playwright já está instalado.")
    except ImportError:
        logging.error("Playwright não está instalado. Por favor, rode: pip install playwright")
        sys.exit(1)
    
    logging.info("Certifique-se de que os navegadores do Playwright estão instalados. Rode 'playwright install' se encontrar erros.")
    
    app = QCoreApplication(sys.argv)
    
    bot_manager = BotManager(bot_id=args.bot_id)
    
    QTimer.singleShot(0, bot_manager.start)
    
    sys.exit(app.exec())