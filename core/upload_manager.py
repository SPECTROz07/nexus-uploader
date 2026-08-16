# -*- coding: utf-8 -*-

import time
import logging
from pathlib import Path
import os

from PySide6.QtCore import QObject, Slot

from core.workers import ChapterVerificationWorker, ChapterUploadTask
from core.processing import natural_sort_key

class VerificationResultHandler(QObject):
    def __init__(self, manga_id, manager, verification_id):
        super().__init__()
        self.manga_id = manga_id
        self.manager = manager
        self.verification_id = verification_id

    @Slot(str, object)
    def handle_result(self, task_id, result):
        self.manager._on_chapters_verified(result, self.manga_id)
        self.manager._cleanup_handler(self.verification_id)

class UploadManager(QObject):
    def __init__(self, api_client, s3_handler, thread_pool):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.api_client = api_client
        self.s3_handler = s3_handler
        self.thread_pool = thread_pool

        self.online_chapters_cache = {}
        self.active_handlers = {}

    def sync_online_cache(self, manga_ids):
        self.logger.info("Sincronizando capítulos existentes de todas as obras monitoradas...")
        for manga_id in manga_ids:
            success, chapters = self.api_client.get_chapters(manga_id)
            if success:
                normalized_chaps = set()
                for c in chapters:
                    num_str = str(c.get('number'))
                    try:
                        num = float(num_str)
                        if num.is_integer():
                            normalized_chaps.add(str(int(num)))
                        else:
                            normalized_chaps.add(str(num))
                    except (ValueError, TypeError):
                        normalized_chaps.add(num_str)
                
                self.online_chapters_cache[str(manga_id)] = normalized_chaps
                self.logger.info(f"  - Obra ID {manga_id}: {len(chapters)} capítulos encontrados online.")
            else:
                self.logger.warning(f"  - Falha ao buscar capítulos para a obra ID {manga_id}.")

    def add_chapters_to_queue(self, chapter_paths, manga_id):
        worker = ChapterVerificationWorker(chapter_paths)
        
        verification_id = f"verify-{manga_id}-{time.time()}"
        result_handler = VerificationResultHandler(manga_id, self, verification_id)
        self.active_handlers[verification_id] = result_handler
        
        worker.signals.finished.connect(result_handler.handle_result)
        worker.signals.error.connect(result_handler.handle_result)
        
        self.thread_pool.start(worker)

    def _cleanup_handler(self, handler_id):
        if handler_id in self.active_handlers:
            del self.active_handlers[handler_id]

    def _on_chapters_verified(self, result, manga_id):
        self.logger.info(f"Resultado da verificação recebido para a Obra ID: {manga_id}")
        
        if not isinstance(result, tuple) or len(result) != 2:
             self.logger.error(f"O worker de verificação retornou um erro ou resultado malformado: {result}")
             return

        valid_chapters, warnings = result
        
        if warnings:
            for warning in warnings:
                clean_warning = warning.replace('<br>', ' ').replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
                self.logger.warning(f"[Verificação] {clean_warning}")

        if not valid_chapters:
            self.logger.warning(f"Nenhuma pasta com imagens válidas retornada pelo worker para Obra ID {manga_id}.")
            return

        chapters_to_upload = []
        online_chaps = self.online_chapters_cache.get(str(manga_id), set())

        for path, data in valid_chapters.items():
            chapter_number = data.get('number')
            if chapter_number is None:
                continue

            chapter_number_str = str(chapter_number)
            if chapter_number_str in online_chaps:
                continue
            
            chapters_to_upload.append({
                'task_id': f"auto-{os.path.basename(path)}-{time.time()}",
                'path': path, 'images': data['images'], 'number': chapter_number_str,
                'access_level': 'public', 'vip_duration_hours': None, 'coin_cost': None
            })

        if not chapters_to_upload:
            self.logger.info(f"Nenhum capítulo NOVO encontrado para a obra {manga_id} após verificação contra o site.")
            return

        sorted_chapters_to_upload = sorted(chapters_to_upload, key=lambda x: natural_sort_key(x['path']))
        
        for chapter_config in sorted_chapters_to_upload:
            self._start_upload_task(chapter_config, manga_id)

    def _start_upload_task(self, chapter_config, manga_id):
        task_id = chapter_config['task_id']
        self.logger.info(f"Iniciando upload para: {os.path.basename(chapter_config['path'])} (Obra ID: {manga_id})")
        
        worker = ChapterUploadTask(
            task_id, self.api_client, self.s3_handler, chapter_config, manga_id
        )
        
        worker.signals.finished.connect(self._on_task_finished)
        worker.signals.error.connect(self._on_task_error)
        worker.signals.progress.connect(self._on_task_progress)
        self.thread_pool.start(worker)

    @Slot(str, object)
    def _on_task_finished(self, task_id, message):
        self.logger.info(f"SUCESSO - Tarefa {task_id}: {message}")

    @Slot(str, object)
    def _on_task_error(self, task_id, error_message):
        self.logger.error(f"FALHA - Tarefa {task_id}: {error_message}")

    @Slot(str, int)
    def _on_task_progress(self, task_id, value):
        if value < 100 and value % 20 == 0: # Loga o progresso a cada 20%
             self.logger.info(f"Progresso - Tarefa {task_id}: {value}%")