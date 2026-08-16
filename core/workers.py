# --- START OF FILE core/workers.py ---

import os
import time
import secrets
import logging
import requests
import re
import shutil
import io
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageFile
from datetime import datetime

from PySide6.QtCore import QObject, Signal, QRunnable, Slot

from core.processing import process_image as process_image_from_path, extract_chapter_number_py, natural_sort_key, process_image_from_bytes

ImageFile.LOAD_TRUNCATED_IMAGES = True

class WorkerSignals(QObject):
    finished = Signal(str, dict)
    error = Signal(str, dict)
    progress = Signal(str, int, str)
    status_update = Signal(str, str)


class GitUpdateWorker(QRunnable):
    """Roda o git pull fora da thread da UI e emite (status, message, commits)."""
    def __init__(self, git_updater):
        super().__init__()
        self.git_updater = git_updater
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            status, message, commits = self.git_updater.check_and_pull()
        except Exception as e:
            status, message, commits = "erro", f"Erro na atualização: {e}", []
        self.signals.finished.emit("git_update", {
            "status": status, "message": message, "commits": commits,
        })

class ChapterVerificationWorker(QRunnable):
    def __init__(self, paths_to_check):
        super().__init__()
        self.paths = paths_to_check
        self.signals = WorkerSignals()
        self.logger = logging.getLogger(self.__class__.__name__)

    def run(self):
        try:
            chapters, all_dirs = {}, []
            valid_ext = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif')
            
            for p_str in self.paths:
                path = Path(p_str)
                if not path.exists(): continue
                if path.is_dir():
                    if any(f.suffix.lower() in valid_ext for f in path.iterdir() if f.is_file()):
                        all_dirs.append(path)
            
            for chapter_dir in sorted(list(set(all_dirs)), key=lambda p: natural_sort_key(p.name)):
                chapters[str(chapter_dir)] = {
                    'number': extract_chapter_number_py(chapter_dir.name),
                    'images': []
                }

            if not chapters:
                payload = {'message': "Nenhuma pasta com imagens suportadas foi encontrada."}
                self.signals.error.emit("verify", payload)
                return

            valid_chapters, warnings = self._verify_chapters(chapters)
            
            payload = {'result': (valid_chapters, warnings)}
            self.signals.finished.emit("verify", payload)

        except Exception as e:
            self.logger.error(f"Erro na verificação de capítulos: {e}", exc_info=True)
            payload = {'message': f"Erro inesperado durante a verificação: {e}"}
            self.signals.error.emit("verify", payload)

    def _verify_chapters(self, chapters):
        valid_chapters, warnings = {}, []
        VERIFY_EXT = ('.jpg', '.jpeg', '.png', '.gif')
        PRE_OPTIMIZED_EXT = ('.webp', '.avif')
        
        for path_str, data in chapters.items():
            path = Path(path_str)
            valid_images, corrupted = [], []
            
            image_files = sorted([f for f in path.iterdir() if f.is_file()], key=lambda x: natural_sort_key(x.name))

            for f_path in image_files:
                ext = f_path.suffix.lower()
                if ext not in VERIFY_EXT + PRE_OPTIMIZED_EXT:
                    continue
                
                if ext in PRE_OPTIMIZED_EXT:
                    valid_images.append(str(f_path))
                    continue

                try:
                    with Image.open(f_path) as img:
                        img.verify()
                        if ext == '.gif':
                            img.seek(0)
                        img.load()
                    valid_images.append(str(f_path))
                except Exception as e:
                    self.logger.warning(f"Imagem corrompida detectada e ignorada: {f_path}. Erro: {e}")
                    corrupted.append(f_path.name)
            
            if corrupted:
                warnings.append(f"<b>Capítulo: {path.name}</b><br> - Imagens ignoradas por corrupção: <i>{', '.join(corrupted)}</i>")
            
            if valid_images:
                valid_chapters[path_str] = {'number': data['number'], 'images': valid_images}
            else:
                warnings.append(f"<b>Capítulo: {path.name}</b><br> - Ignorado (nenhuma imagem válida encontrada).")
        
        return valid_chapters, warnings
        
class ChapterUploadTask(QRunnable):
    def __init__(self, task_id, api_client, s3_handler, chapter_config, manga_id=None, temp_dir_to_clean=None):
        super().__init__()
        self.signals = WorkerSignals()
        self.task_id = task_id
        self.api_client = api_client
        self.s3_handler = s3_handler
        self.config = chapter_config
        self.manga_id = manga_id if manga_id is not None else chapter_config.get('manga_id')
        self._is_cancelled = False
        self.max_retries = 3
        self.logger = logging.getLogger(self.__class__.__name__)
        self.temp_dir_to_clean = temp_dir_to_clean

    def cancel(self):
        self._is_cancelled = True
        self.signals.status_update.emit(self.task_id, "Cancelando...")

    def run(self):
        if scheduled_time_str := self.config.get('scheduled_time'):
            try:
                scheduled_time = datetime.fromisoformat(scheduled_time_str)
                now = datetime.now()
                if scheduled_time > now:
                    wait_seconds = (scheduled_time - now).total_seconds()
                    status_msg = f"Agendado para {scheduled_time.strftime('%d/%m %H:%M')}"
                    self.signals.status_update.emit(self.task_id, status_msg)
                    
                    for _ in range(int(wait_seconds)):
                        if self._is_cancelled:
                            raise Exception("Cancelado pelo usuário antes do início agendado.")
                        time.sleep(1)
            except (ValueError, TypeError):
                self.logger.error(f"Formato de data/hora agendada inválido para a tarefa {self.task_id}: {scheduled_time_str}")

        chapter_id_created = None
        last_exception = None

        try:
            for attempt in range(self.max_retries):
                if self._is_cancelled:
                    last_exception = Exception("Cancelado pelo usuário.")
                    break
                try:
                    status_msg = f"Iniciando (tentativa {attempt + 1}/{self.max_retries})"
                    self.signals.progress.emit(self.task_id, 1, status_msg)

                    api_chapter_config = {
                        'chapter_number': str(self.config['number']), 'title': self.config.get('title', ''),
                        'access_level': self.config.get('access_level', 'public'),
                        'vip_duration_hours': self.config.get('vip_duration_hours'), 'coin_cost': self.config.get('coin_cost')
                    }
                    success, response = self.api_client.create_or_get_chapter(self.manga_id, api_chapter_config)
                    if not success:
                        raise Exception(f"API (criar cap): {response}")
                    
                    chapter_id_created = response['chapter_id']
                    s3_base_key = f"manga_pages/{self.manga_id}/{chapter_id_created}"

                    # Modo substituição: guarda as páginas antigas do capítulo
                    # pra removê-las do R2 depois que as novas assumirem.
                    paginas_antigas = []
                    if self.config.get('overwrite'):
                        listadas = self.s3_handler.list_chapter_files(s3_base_key) or []
                        # list devolve a chave completa; delete_files re-adiciona o
                        # path_prefix, entao ele e tirado aqui pra nao duplicar
                        prefixo = f"{self.s3_handler.path_prefix}/" if self.s3_handler.path_prefix else ""
                        paginas_antigas = [obj['Key'][len(prefixo):] if prefixo and obj['Key'].startswith(prefixo) else obj['Key']
                                           for obj in listadas]

                    image_paths = [str(p) for p in self.config['images']]
                    uploaded_paths = []
                    total_images = len(image_paths)
                    
                    upload_msg = "Processando e enviando imagens..."
                    self.signals.progress.emit(self.task_id, 5, upload_msg)
                    
                    last_progress_emitted = 5
                    for i, img_path in enumerate(image_paths):
                        if self._is_cancelled: raise Exception("Cancelado durante o upload.")
                        
                        processed_parts = process_image_from_path(img_path)
                        for filename, file_bytes in processed_parts:
                            if self._is_cancelled: break
                            
                            _base, ext = os.path.splitext(filename)
                            random_hex = secrets.token_hex(4)
                            safe_filename = f"page_{i+1:03d}_{random_hex}{ext.lower()}"
                            object_key = f"{s3_base_key}/{safe_filename}"
                            
                            success, result_data = self.s3_handler.upload_file_obj(BytesIO(file_bytes), object_key)
                            if not success: 
                                raise Exception(f"S3/R2 (upload): {result_data}")
                            
                            uploaded_paths.append(result_data["object_key"])

                        current_progress = min(90, int(((i + 1) / total_images) * 85) + 5)
                        if current_progress > last_progress_emitted:
                            self.signals.progress.emit(self.task_id, current_progress, upload_msg)
                            last_progress_emitted = current_progress

                    finalize_msg = "Finalizando com o servidor..."
                    self.signals.progress.emit(self.task_id, 95, finalize_msg)
                    
                    success, response = self.api_client.update_chapter_with_pages(chapter_id_created, uploaded_paths)
                    if not success: raise Exception(f"API (associar pág): {response}")

                    if paginas_antigas:
                        novas = set(uploaded_paths)
                        sobras = [k for k in paginas_antigas if k not in novas]
                        if sobras:
                            ok_limpeza, msg_limpeza = self.s3_handler.delete_files(sobras)
                            if ok_limpeza:
                                self.logger.info(f"Substituição: {len(sobras)} página(s) antiga(s) removida(s) do R2.")
                            else:
                                self.logger.warning(f"Substituição concluída, mas a limpeza das páginas antigas falhou: {msg_limpeza}")

                    payload = {'message': "Concluído com sucesso"}
                    self.signals.finished.emit(self.task_id, payload)
                    return
                
                except Exception as e:
                    last_exception = e
                    self.logger.error(f"Erro detalhado na tarefa {self.task_id} (tentativa {attempt + 1})", exc_info=True)
                    self.logger.warning(f"Tentativa {attempt + 1}/{self.max_retries} falhou para a tarefa {self.task_id}: {e}")
                    if attempt < self.max_retries - 1 and not self._is_cancelled:
                        self.signals.status_update.emit(self.task_id, f"Erro. Tentando novamente em {(attempt + 1) * 2}s...")
                        time.sleep((attempt + 1) * 2)
                    continue
            
            message = str(last_exception)
            
            if chapter_id_created and "Cancelado" not in message:
                self.logger.error(f"Erro final na tarefa {self.task_id}: {message}. Tentando limpar capítulo fantasma (ID: {chapter_id_created})...", exc_info=True)
                self.signals.status_update.emit(self.task_id, "Erro. Tentando limpar o servidor...")
                cleanup_success, cleanup_msg = self.api_client.delete_chapter(chapter_id_created)
                if cleanup_success:
                    message += " (Capítulo órfão removido do servidor)"
                else:
                    message += " (Falha ao remover capítulo órfão)"
            
            elif not self._is_cancelled:
                self.logger.error(f"Erro irrecuperável na tarefa {self.task_id}: {message}", exc_info=True)

            payload = {'message': message}
            self.signals.error.emit(self.task_id, payload)
        finally:
            if self.temp_dir_to_clean and os.path.exists(self.temp_dir_to_clean):
                try:
                    shutil.rmtree(self.temp_dir_to_clean)
                    self.logger.info(f"Pasta temporária do provider {self.temp_dir_to_clean} limpa com sucesso.")
                except Exception as e:
                    self.logger.error(f"Erro ao limpar pasta temporária {self.temp_dir_to_clean}: {e}")

class ChapterDeleteWorker(QRunnable):
    """Apaga capítulos publicados: registro na API + arquivos no R2."""

    def __init__(self, api_client, s3_handler, manga_id, chapters):
        super().__init__()
        self.signals = WorkerSignals()
        self.api_client = api_client
        self.s3_handler = s3_handler
        self.manga_id = manga_id
        self.chapters = chapters
        self.logger = logging.getLogger(self.__class__.__name__)

    @Slot()
    def run(self):
        apagados, falhas = [], []
        for c in self.chapters:
            chapter_id = c.get('id')
            numero = str(c.get('number', '?'))
            if not chapter_id:
                falhas.append(f"{numero}: sem id")
                continue
            ok, resp = self.api_client.delete_chapter(chapter_id)
            if not ok:
                falhas.append(f"{numero}: {resp}")
                continue
            apagados.append(numero)
            try:
                self.s3_handler.delete_folder(f"manga_pages/{self.manga_id}/{chapter_id}")
            except Exception as e:
                # o registro já saiu do site; sobra órfão no R2, só loga
                self.logger.warning(f"Capítulo {numero} apagado da API, mas falhou a limpeza no R2: {e}")
        self.signals.finished.emit("delete_chapters", {
            'manga_id': str(self.manga_id), 'apagados': apagados, 'falhas': falhas,
        })


class MangaUpdateWorker(QRunnable):
    def __init__(self, api_client, manga_data, manga_id=None):
        super().__init__()
        self.api_client = api_client
        self.manga_data = manga_data
        self.manga_id = manga_id
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            action = "Atualizando" if self.manga_id else "Criando"
            self.signals.status_update.emit("manga_update", f"{action} dados da obra na API...")
            
            if self.manga_id:
                success, response = self.api_client.update_manga(self.manga_id, self.manga_data)
            else:
                success, response = self.api_client.create_manga(self.manga_data)
            
            if success:
                self.signals.status_update.emit("manga_update", "Operação concluída com sucesso!")
                final_response = response if isinstance(response, dict) else self.manga_data
                self.signals.finished.emit("manga_update", final_response)
            else:
                raise Exception(response)

        except Exception as e:
            self.signals.error.emit("manga_update", {'message': str(e)})
        
class MangaDownloadWorker(QRunnable):
    def __init__(self, task_id, api_client, manga_id, manga_title, dest_path):
        super().__init__()
        self.signals = WorkerSignals()
        self.task_id = task_id
        self.api_client = api_client
        self.manga_id = manga_id
        self.manga_title = manga_title
        self.dest_path = Path(dest_path)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        self.signals.status_update.emit(self.task_id, "Cancelando...")

    def _sanitize_filename(self, name):
        name = str(name)
        return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

    def run(self):
        try:
            self.signals.progress.emit(self.task_id, 1, "Buscando lista de capítulos...")
            
            manga_folder = self.dest_path / self._sanitize_filename(self.manga_title)
            manga_folder.mkdir(parents=True, exist_ok=True)
            success, chapters = self.api_client.get_chapters(self.manga_id)
            if not success:
                raise Exception(f"Não foi possível obter a lista de capítulos: {chapters}")
            if not chapters:
                payload = {'message': "Nenhum capítulo encontrado online."}
                self.signals.finished.emit(self.task_id, payload)
                return
            sorted_chapters = sorted(chapters, key=lambda c: natural_sort_key(c.get('number', '0')))
            total_chapters = len(sorted_chapters)
            for i, chapter_data in enumerate(sorted_chapters):
                if self._is_cancelled: raise Exception("Download cancelado pelo usuário.")
                chapter_number = chapter_data.get('number', f'desconhecido_{i+1}')
                chapter_id = chapter_data.get('id')
                progress = int(((i + 1) / total_chapters) * 100)
                status_msg = f"Baixando cap. {chapter_number} ({i+1}/{total_chapters})"
                
                self.signals.progress.emit(self.task_id, progress, status_msg)

                if not chapter_id: continue
                success, details = self.api_client.get_chapter_details(chapter_id)
                if not success: continue
                chapter_folder_name = self._sanitize_filename(f"Capítulo {chapter_number}")
                chapter_folder = manga_folder / chapter_folder_name
                chapter_folder.mkdir(exist_ok=True)
                pages = details.get('pages', [])
                if not pages: continue
                for page_index, page_data in enumerate(pages):
                    if self._is_cancelled: break
                    page_url = page_data.get('image_url')
                    if not page_url: continue
                    try:
                        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                        res = requests.get(page_url, stream=True, timeout=30, headers=headers)
                        res.raise_for_status()
                        content_type = res.headers.get('Content-Type', '')
                        if 'jpeg' in content_type or 'jpg' in content_type: ext = '.jpg'
                        elif 'png' in content_type: ext = '.png'
                        elif 'webp' in content_type: ext = '.webp'
                        elif 'avif' in content_type: ext = '.avif'
                        else: ext = Path(page_url).suffix.split('?')[0] or '.jpg'
                        filepath = chapter_folder / f"{page_index + 1:03d}{ext}"
                        with open(filepath, 'wb') as f:
                            f.write(res.content)
                    except requests.RequestException as e:
                        self.logger.error(f"Erro ao baixar a página {page_url}: {e}")
            if self._is_cancelled: raise Exception("Download cancelado pelo usuário.")
            payload = {'message': "Download Concluído com Sucesso"}
            self.signals.finished.emit(self.task_id, payload)
        except Exception as e:
            self.logger.error(f"Erro no download da obra {self.manga_id}: {e}", exc_info=True)
            payload = {'message': str(e)}
            self.signals.error.emit(self.task_id, payload)

class MangaListFetcher(QRunnable):
    def __init__(self, api_client, query):
        super().__init__()
        self.api_client = api_client
        self.query = query
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            success, data = self.api_client.get_mangas(self.query, timeout=20)
            if success:
                self.signals.finished.emit("manga_list", data)
            else:
                self.signals.error.emit("manga_list", {'message': str(data)})
        except Exception as e:
            self.signals.error.emit("manga_list", {'message': f"Erro de conexão: {e}"})

class LoginWorker(QRunnable):
    def __init__(self, api_client, username, password, s3_config_path):
        super().__init__()
        self.api_client = api_client
        self.username = username
        self.password = password
        self.s3_config_path = s3_config_path
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        from core.s3_handler import ContaboS3Handler
        try:
            login_success, login_response = self.api_client.login(self.username, self.password)
            if not login_success:
                raise Exception(login_response)

            details_success, user_data = self.api_client.get_current_user_details()
            # o backend responde is_staff (snake) ou isStaff (camel), conforme a rota
            is_staff = bool(user_data.get('is_staff') or user_data.get('isStaff')
                            or user_data.get('is_superuser') or user_data.get('isSuperuser')) \
                if isinstance(user_data, dict) else False
            if not details_success or not is_staff:
                raise Exception("Acesso negado. A conta não tem permissão de staff.")

            temp_s3_handler = ContaboS3Handler(config_path=self.s3_config_path)
            s3_success, s3_msg = temp_s3_handler.test_connection()
            if not s3_success:
                raise Exception(f"Falha na conexão com o Storage: {s3_msg}")

            self.signals.finished.emit("login", user_data)
        except Exception as e:
            self.signals.error.emit("login", {'message': str(e)})

class ChapterDetailsFetcher(QRunnable):
    def __init__(self, api_client, manga_id):
        super().__init__()
        self.api_client = api_client
        self.manga_id = manga_id
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            success, data = self.api_client.get_chapters(self.manga_id)
            payload = {'manga_id': str(self.manga_id), 'chapters': data}
            if success:
                self.signals.finished.emit("chapter_details", payload)
            else:
                self.signals.error.emit("chapter_details", {'message': str(data), 'manga_id': str(self.manga_id)})
        except Exception as e:
            self.signals.error.emit("chapter_details", {'message': f"Erro de conexão: {e}", 'manga_id': str(self.manga_id)})

class CacheMangaDetailsWorker(QRunnable):
    def __init__(self, api_client, manga_ids_to_fetch):
        super().__init__()
        self.api_client = api_client
        self.manga_ids = manga_ids_to_fetch
        self.signals = WorkerSignals()

    def run(self):
        try:
            manga_details_cache = {}
            total_to_fetch = len(self.manga_ids)
            for i, manga_id in enumerate(self.manga_ids):
                progress_text = f"Carregando detalhes... {i + 1}/{total_to_fetch}"
                self.signals.status_update.emit("cache_progress", f"{i+1};{total_to_fetch};{progress_text}")
                
                success, data = self.api_client.get_manga_details(manga_id)
                if success:
                    self.signals.finished.emit("single_manga_detail", {'manga_id': str(manga_id), 'data': data})
                    manga_details_cache[str(manga_id)] = data
                else:
                    manga_details_cache[str(manga_id)] = {"title": f"Falha ao carregar ID: {manga_id}"}
            self.signals.finished.emit("cache_complete", {'cache': manga_details_cache})
        except Exception as e:
            self.signals.error.emit("cache", {'message': str(e)})

class CategoryFetcher(QRunnable):
    def __init__(self, api_client):
        super().__init__()
        self.api_client = api_client
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            success, data = self.api_client.get_all_categories_paginated()
            if success and isinstance(data, list):
                self.signals.finished.emit("categories", {'categories': data})
            else:
                self.signals.error.emit("categories", {'message': str(data) if data else "Resposta inválida da API."})
        except Exception as e:
            self.signals.error.emit("categories", {'message': f"Erro de conexão: {e}"})

class CoverUploadWorker(QRunnable):
    def __init__(self, s3_handler, path_or_url, manga_title, manga_id=None):
        super().__init__()
        self.s3_handler = s3_handler
        self.path_or_url = path_or_url
        self.manga_title = manga_title
        self.manga_id = manga_id
        self.signals = WorkerSignals()
        self.logger = logging.getLogger(self.__class__.__name__)

    @Slot()
    def run(self):
        try:
            self.signals.status_update.emit("cover_upload", "Carregando imagem...")
            image_bytes, filename = self._get_image_bytes()
            
            self.signals.status_update.emit("cover_upload", "Processando e otimizando a imagem da capa...")
            processed_filename, processed_bytes = process_image_from_bytes(filename, image_bytes)

            safe_title = re.sub(r'[^a-zA-Z0-9_-]', '', self.manga_title).lower() or "manga"

            manga_id_folder = f"manga_{self.manga_id}" if self.manga_id else "manga_new"
            
            relative_object_key = (
                f"manga_covers/{manga_id_folder}/"
                f"{safe_title}_{secrets.token_hex(4)}{os.path.splitext(processed_filename)[1]}"
            )
            
            self.signals.status_update.emit("cover_upload", "Enviando imagem da capa para o servidor...")
            success, result_data = self.s3_handler.upload_file_obj(io.BytesIO(processed_bytes), relative_object_key)

            if success and isinstance(result_data, dict) and 'object_key' in result_data:
                payload = {
                    'object_key': result_data['object_key']
                }
                self.signals.finished.emit("cover_upload", payload)
            else:
                raise Exception(f"Falha no upload para o S3/R2: {result_data}")
        except Exception as e:
            self.logger.error(f"Erro no CoverUploadWorker: {e}", exc_info=True)
            self.signals.error.emit("cover_upload", {'message': str(e)})

    def _get_image_bytes(self):
        if self.path_or_url.startswith(('http://', 'https://')):
            response = requests.get(self.path_or_url, stream=True, timeout=30)
            response.raise_for_status()
            filename = os.path.basename(self.path_or_url.split('?')[0]) or "cover.jpg"
            return response.content, filename
        else:
            filename = os.path.basename(self.path_or_url)
            with open(self.path_or_url, 'rb') as f:
                return f.read(), filename

class ChapterExistenceVerifier(QRunnable):
    def __init__(self, task_id, api_client, manga_id, chapter_number):
        super().__init__()
        self.signals = WorkerSignals()
        self.task_id = task_id
        self.api_client = api_client
        self.manga_id = manga_id
        self.chapter_number = str(chapter_number)
        self.max_retries = 3
        self.retry_delay = 5

    @Slot()
    def run(self):
        try:
            for attempt in range(self.max_retries):
                self.signals.status_update.emit(self.task_id, f"Verificando... ({attempt + 1}/{self.max_retries})")
                
                success, chapters_list = self.api_client.get_chapter_numbers_list(self.manga_id)
                
                if success and isinstance(chapters_list, list):
                    online_chapters = set(map(str, chapters_list))
                    if self.chapter_number in online_chapters:
                        self.signals.finished.emit(self.task_id, {'message': "Verificado com sucesso"})
                        return
                
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
            
            raise Exception("O capítulo não foi encontrado na API após o upload.")

        except Exception as e:
            self.signals.error.emit(self.task_id, {'message': f"Falha na verificação: {e}"})

class TaskEnqueuingWorker(QRunnable):
    def __init__(self, chapters_to_process, manga_id, manga_title):
        super().__init__()
        self.signals = WorkerSignals() 
        self.chapters = chapters_to_process
        self.manga_id = manga_id
        self.manga_title = manga_title

    @Slot()
    def run(self):
        try:
            processed_tasks = []
            for config in self.chapters:
                final_config = config.copy() 
                final_config['manga_id'] = self.manga_id
                final_config['manga_title'] = self.manga_title
                processed_tasks.append(final_config)

            payload = {'tasks': processed_tasks, 'manga_id': self.manga_id, 'manga_title': self.manga_title}
            self.signals.finished.emit("enqueue_ready", payload)

        except Exception as e:
            self.signals.error.emit("enqueue_ready", {'message': f"Erro ao preparar tarefas: {e}"})