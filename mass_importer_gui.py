import sys
import os
import time
import re
import io
import secrets
import importlib
import shutil
from pathlib import Path
import json
from urllib.parse import urlparse
import importlib.util
from collections import deque
import base64

if getattr(sys, 'frozen', False):
    base_path = Path(sys.executable).parent
else:
    base_path = Path(__file__).parent
if str(base_path) not in sys.path:
    sys.path.insert(0, str(base_path))
assets_path = base_path / "assets"
if str(assets_path) not in sys.path:
    sys.path.insert(0, str(assets_path))

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Type

import requests
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QListWidget, QListWidgetItem,
    QListView, QProgressDialog, QAbstractItemView, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QPlainTextEdit, QInputDialog, QFileDialog,
    QMenuBar, QTabWidget
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QRunnable, QThreadPool, Slot, QSize, QTimer
)
from PySide6.QtGui import (
    QColor, QPixmap, QIcon, QFont, QAction, QTextCursor
)

from core.api_client import ApiClient
from core.credentials import CredentialsManager
from core.s3_handler import ContaboS3Handler
from core.settings_manager import SettingsManager
from core.processing import process_image_from_bytes
from ui.theme import AppTheme
from providers_obra.base_obra import BaseObraProvider

class QtLogHandler(logging.Handler, QObject):
    new_log_record = Signal(str)
    def __init__(self):
        super().__init__()
        QObject.__init__(self)
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    def emit(self, record):
        msg = self.format(record)
        self.new_log_record.emit(msg)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def discover_obra_providers() -> Dict[str, Type[BaseObraProvider]]:
    providers = {}
    providers_dir = base_path / "providers_obra"
    if not providers_dir.is_dir():
        providers_dir.mkdir(parents=True, exist_ok=True)
    init_file = providers_dir / "__init__.py"
    if not init_file.exists():
        init_file.touch()
    # cobre fonte (.py) e compilado (.pyd, ex: maidscan_obra.cp313-win_amd64.pyd)
    vistos = set()
    arquivos = list(providers_dir.glob("*_obra.py")) + list(providers_dir.glob("*_obra*.pyd"))
    for f in arquivos:
        provider_name = f.name.split("_obra", 1)[0]
        modulo_stem = f"{provider_name}_obra"
        if provider_name in ["base", "base_madara"] or modulo_stem in vistos:
            continue
        vistos.add(modulo_stem)
        try:
            module_name = f"providers_obra.{modulo_stem}"
            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)
            for item_name in dir(module):
                item = getattr(module, item_name)
                if isinstance(item, type) and issubclass(item, BaseObraProvider) and item is not BaseObraProvider:
                    providers[provider_name] = item
                    logger.info(f"Provider '{provider_name}' carregado da classe '{item_name}'.")
                    break
        except Exception as e:
            logger.error(f"Erro ao carregar provider '{provider_name}': {e}", exc_info=True)
    return providers

def discover_chapter_providers():
    # reusa o registry, que já cobre .py (via ast) e .pyd (via manifest.json)
    try:
        from core.provider_registry import listar_providers
        return sorted(p.nome for p in listar_providers())
    except Exception:
        logger.error("Falha ao listar providers via registry", exc_info=True)
        return []

@dataclass
class MangaImportData:
    title: str
    source_url: str
    cover_url: str = ""
    cover_data: str = None
    cover_pixmap: QPixmap = None
    status: str = "Aguardando verificação..."
    details: dict = field(default_factory=dict)
    api_response: dict = field(default_factory=dict)
    list_item: QListWidgetItem = None

class WorkerSignals(QObject):
    finished = Signal(str, dict)
    error = Signal(str, str)
    progress = Signal(str)
    manga_found = Signal(object)
    existence_checked = Signal(str, object)

class MangaListScraper(QRunnable):
    def __init__(self, provider_name: str, provider_classes: Dict[str, Type[BaseObraProvider]], start_url: str):
        super().__init__()
        self.signals = WorkerSignals()
        self.provider_name = provider_name
        self.provider_classes = provider_classes
        self.start_url = start_url
    
    def run(self):
        try:
            ProviderClass = self.provider_classes.get(self.provider_name)
            if not ProviderClass:
                raise ValueError(f"Provider '{self.provider_name}' não foi encontrado.")
            provider = ProviderClass()
            self.signals.progress.emit("Buscando lista de obras...")
            logger.info(f"Iniciando busca com o provider '{self.provider_name}'...")
            manga_list = provider.get_manga_list(self.start_url)
            for manga_data in manga_list:
                manga = MangaImportData(
                    title=manga_data['title'],
                    source_url=manga_data['url'],
                    cover_url=manga_data.get('cover_url', '')
                )
                self.signals.manga_found.emit(manga)
            logger.info(f"Busca de lista concluída. {len(manga_list)} obras encontradas.")
            self.signals.finished.emit("list_scraper", {})
        except Exception as e:
            logger.error(f"Erro no MangaListScraper: {e}", exc_info=True)
            self.signals.error.emit("list_scraper", f"Falha ao buscar lista: {e}")

class MangaExistenceChecker(QRunnable):
    def __init__(self, api_client: ApiClient, manga: MangaImportData):
        super().__init__()
        self.signals = WorkerSignals()
        self.api_client = api_client
        self.manga = manga
    def run(self):
        try:
            success, data = self.api_client.get_mangas(self.manga.title)
            if not success:
                self.signals.existence_checked.emit(self.manga.source_url, None)
                return
            found_manga = next((m for m in data.get('results', []) if m['title'].lower() == self.manga.title.lower()), None)
            self.signals.existence_checked.emit(self.manga.source_url, found_manga)
        except Exception:
            self.signals.existence_checked.emit(self.manga.source_url, None)

class FullProcessWorker(QRunnable):
    def __init__(self, provider_name: str, provider_classes: Dict[str, Type[BaseObraProvider]], manga: MangaImportData, api_client: ApiClient, s3_handler: ContaboS3Handler, category_map: dict, task_type: str, task_args: tuple, nsfw_keywords: set):
        super().__init__()
        self.signals = WorkerSignals()
        self.provider_name = provider_name
        self.provider_classes = provider_classes
        self.manga = manga
        self.api_client = api_client
        self.s3_handler = s3_handler
        self.category_map = category_map
        self.task_type = task_type
        self.task_args = task_args
        self.nsfw_keywords = nsfw_keywords
        self.provider = None
    
    def _get_cover_bytes(self):
        try:
            if not self.manga.cover_url:
                raise ValueError("URL da capa não foi definida. Detalhes não foram buscados?")
            logger.info(f"Baixando capa de alta resolução: {self.manga.cover_url}")
            response = requests.get(self.manga.cover_url, timeout=30, headers={'Referer': self.manga.source_url})
            response.raise_for_status()
            return True, response.content
        except Exception as e:
            logger.error(f"Erro ao obter bytes da capa '{self.manga.cover_url}': {e}", exc_info=True)
            return False, None
    
    def run(self):
        try:
            ProviderClass = self.provider_classes.get(self.provider_name)
            if not ProviderClass:
                raise ValueError(f"Provider '{self.provider_name}' não foi encontrado.")
            self.provider = ProviderClass()
            if self.task_type == "registration":
                self._run_registration()
            elif self.task_type == "update":
                self._run_update()
            elif self.task_type == "full_update":
                self._run_full_update()
        except Exception as e:
            logger.error(f"Erro no FullProcessWorker para {self.manga.source_url}: {e}", exc_info=True)
            self.signals.error.emit(self.manga.source_url, str(e))
    
    def _get_details_and_process_categories(self):
        logger.info(f"Buscando detalhes para '{self.manga.title}'...")
        details = self.provider.get_manga_details(self.manga.source_url)
        self.manga.details = details
        category_ids = []
        for genre_name in details.get('genres', []):
            genre_name_lower = genre_name.lower()
            if genre_name_lower in self.category_map:
                category_ids.append(self.category_map[genre_name_lower])
                continue
            logger.warning(f"Categoria '{genre_name}' não encontrada. Tentando criar...")
            success, response = self.api_client.create_category(genre_name)
            if success and response.get('id'):
                new_id = response['id']
                logger.info(f"Categoria '{genre_name}' criada com sucesso (ID: {new_id}).")
                self.category_map[genre_name_lower] = new_id
                category_ids.append(new_id)
            else:
                logger.error(f"Falha ao criar a categoria '{genre_name}'. Resposta: {response}")
        return details, category_ids

    def _run_registration(self):
        work_type, custom_work_type = self.task_args
        logger.info(f"Iniciando cadastro de '{self.manga.title}'...")
        details, category_ids = self._get_details_and_process_categories()
        self.manga.cover_url = details.get('cover_url', '')
        if not self.manga.cover_url:
            raise ValueError("Não foi possível encontrar a URL da capa na página da obra.")
        is_nsfw = any(keyword in {g.lower() for g in details.get('genres', [])} for keyword in self.nsfw_keywords)
        success, cover_bytes = self._get_cover_bytes()
        if not success:
            raise Exception("Falha ao obter bytes da capa")
        _, processed_bytes = process_image_from_bytes("cover.webp", cover_bytes, lossless=True)
        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '', self.manga.title).lower() or "manga"
        object_key = f"manga_covers/manga_new/{safe_title}_{secrets.token_hex(4)}.webp"
        success, result = self.s3_handler.upload_file_obj(io.BytesIO(processed_bytes), object_key)
        if not success:
            raise Exception(f"S3: {result}")
        payload = {"title": self.manga.title, "description": details.get('description', ''), "author": details.get('author', ''), "artist": details.get('artist', ''), "status": details.get('status', 'ongoing'), "work_type": work_type, "custom_work_type": custom_work_type, "is_nsfw": is_nsfw, "cover_image_url": result['object_key'], "categories": category_ids}
        success, api_resp = self.api_client.create_manga(payload)
        if not success:
            raise Exception(f"API: {api_resp}")
        logger.info(f"Obra '{self.manga.title}' cadastrada (ID: {api_resp.get('id')}). NSFW: {is_nsfw}")
        self.signals.finished.emit(self.manga.source_url, api_resp)

    def _run_update(self):
        new_work_type, new_custom_work_type = self.task_args
        manga_id = self.manga.api_response.get('id')
        if not manga_id:
            raise Exception("ID da obra não encontrado para atualização.")
        payload = {"work_type": new_work_type, "custom_work_type": new_custom_work_type}
        success, api_resp = self.api_client.update_manga(manga_id, payload)
        if not success:
            raise Exception(f"API: {api_resp}")
        logger.info(f"Obra '{self.manga.title}' atualizada com sucesso.")
        self.signals.finished.emit(self.manga.source_url, api_resp)
        
    def _run_full_update(self):
        manga_id = self.manga.api_response.get('id')
        if not manga_id:
            raise Exception("ID da obra não encontrado para atualização.")
            
        details, category_ids = self._get_details_and_process_categories()
        
        self.manga.cover_url = details.get('cover_url', '')
        if not self.manga.cover_url:
            raise ValueError("Não foi possível encontrar a URL da capa na página da obra para atualizar.")
            
        is_nsfw = any(keyword in {g.lower() for g in details.get('genres', [])} for keyword in self.nsfw_keywords)
        
        logger.info(f"Atualizando capa para '{self.manga.title}'...")
        success, cover_bytes = self._get_cover_bytes()
        if not success:
            raise Exception("Falha ao obter bytes da nova capa para atualização.")
        
        _, processed_bytes = process_image_from_bytes("cover.webp", cover_bytes, lossless=True)
        
        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '', self.manga.title).lower() or "manga"
        object_key = f"manga_covers/manga_{manga_id}/{safe_title}_{secrets.token_hex(4)}.webp"
        
        success, result = self.s3_handler.upload_file_obj(io.BytesIO(processed_bytes), object_key)
        if not success:
            raise Exception(f"S3 (atualizar capa): {result}")
        
        logger.info(f"Nova capa para '{self.manga.title}' enviada para o S3.")
        
        # --- INÍCIO DA CORREÇÃO ---
        # O payload agora inclui a chave 'cover_image_url' com o novo link do S3.
        payload = {
            "description": details.get('description', ''),
            "author": details.get('author', ''),
            "artist": details.get('artist', ''),
            "status": details.get('status', 'ongoing'),
            "is_nsfw": is_nsfw,
            "categories": category_ids,
            "cover_image_url": result['object_key'] # ESTA LINHA ESTAVA FALTANDO
        }
        # --- FIM DA CORREÇÃO ---
        
        success, api_resp = self.api_client.update_manga(manga_id, payload)
        if not success:
            raise Exception(f"API: {api_resp}")
            
        logger.info(f"Dados e capa da obra '{self.manga.title}' atualizados. NSFW: {is_nsfw}")
        self.signals.finished.emit(self.manga.source_url, api_resp)

class MangaDetailsDialog(QDialog):
    def __init__(self, manga: MangaImportData, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detalhes da Obra")
        self.setMinimumSize(600, 500)
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        title_label = QLabel(manga.title)
        title_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title_label.setWordWrap(True)
        layout.addWidget(title_label)
        if manga.api_response:
            form_layout.addRow("ID no Site:", QLineEdit(str(manga.api_response.get('id', 'N/A')), readOnly=True))
            work_type = manga.api_response.get('work_type', 'N/A')
            display_type = f"Outro ({manga.api_response.get('custom_work_type', '')})" if work_type == 'other' else work_type
            form_layout.addRow("Tipo no Site:", QLineEdit(display_type, readOnly=True))
        form_layout.addRow("URL de Origem:", QLineEdit(manga.source_url, readOnly=True))
        form_layout.addRow("Autor(es):", QLineEdit(manga.details.get('author', 'N/A'), readOnly=True))
        form_layout.addRow("Artista(s):", QLineEdit(manga.details.get('artist', 'N/A'), readOnly=True))
        form_layout.addRow("Gêneros:", QLineEdit(", ".join(manga.details.get('genres', [])), readOnly=True))
        form_layout.addRow("Status:", QLineEdit(manga.details.get('status', 'N/A').capitalize(), readOnly=True))
        self.description_edit = QPlainTextEdit(manga.details.get('description', 'N/A'))
        self.description_edit.setReadOnly(True)
        form_layout.addRow("Sinopse:", self.description_edit)
        layout.addLayout(form_layout)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)

class MassImporterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus Cadastro")
        self.setGeometry(100, 100, 1200, 800)
        
        self.settings_manager = SettingsManager()
        icon_path = assets_path / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        
        self.mangas: Dict[str, MangaImportData] = {}
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(8)
        
        self.obra_providers: Dict[str, Type[BaseObraProvider]] = {}
        self.chapter_providers = []

        self.check_queue = deque()
        self.task_queue = deque()

        self.log_handler = QtLogHandler()
        logger.addHandler(self.log_handler)

        self._init_backend()
        self._setup_ui()
        self.reload_providers()
        
    def _setup_ui(self):
        self._create_menu()
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        
        tools_layout = QHBoxLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://site.com/manga  OU  https://site.com/manga/nome-da-obra")
        self.fetch_button = QPushButton("Buscar Lista")
        self.fetch_button.clicked.connect(self.start_list_scraping)
        self.fetch_single_button = QPushButton("Buscar Obra Única")
        self.fetch_single_button.clicked.connect(self.start_single_scraping)
        
        tools_layout.addWidget(QLabel("Provider:"))
        tools_layout.addWidget(self.provider_combo)
        tools_layout.addWidget(QLabel("URL:"))
        tools_layout.addWidget(self.url_input, 2)
        tools_layout.addWidget(self.fetch_button)
        tools_layout.addWidget(self.fetch_single_button)
        self.layout.addLayout(tools_layout)

        filter_layout = QHBoxLayout()
        self.nsfw_filter_input = QLineEdit()
        self.nsfw_filter_input.setPlaceholderText("adulto, nsfw, smut, 18+ ...")
        self.nsfw_filter_input.editingFinished.connect(self.save_nsfw_filter)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filtrar por nome...")
        self.filter_input.textChanged.connect(self.filter_list)

        filter_layout.addWidget(QLabel("Filtro NSFW (separado por vírgula):"))
        filter_layout.addWidget(self.nsfw_filter_input, 1)
        filter_layout.addWidget(QLabel("Filtrar:"))
        filter_layout.addWidget(self.filter_input, 1)
        self.layout.addLayout(filter_layout)

        self.tab_widget = QTabWidget()
        self.layout.addWidget(self.tab_widget)
        
        self.manga_tab = self._create_manga_tab()
        self.log_tab = self._create_log_tab()
        
        self.tab_widget.addTab(self.manga_tab, "Obras Encontradas")
        self.tab_widget.addTab(self.log_tab, "Log de Atividades")

        actions_layout = QHBoxLayout()
        self.select_all_button = QPushButton("Marcar Visíveis")
        self.select_all_button.clicked.connect(lambda: self.toggle_selection(True))
        self.unselect_all_button = QPushButton("Desmarcar Visíveis")
        self.unselect_all_button.clicked.connect(lambda: self.toggle_selection(False))
        self.update_data_button = QPushButton("Atualizar Dados")
        self.update_data_button.clicked.connect(self.start_full_update)
        self.update_type_button = QPushButton("Atualizar Tipo")
        self.update_type_button.clicked.connect(self.start_type_update)
        self.register_button = QPushButton("Cadastrar Pendentes")
        self.register_button.clicked.connect(self.start_registration)
        self.generate_button = QPushButton("Gerar Monitoramento")
        self.generate_button.clicked.connect(self.generate_monitoring_file)
        self.status_label = QLabel("Pronto.")
        
        actions_layout.addWidget(self.select_all_button)
        actions_layout.addWidget(self.unselect_all_button)
        actions_layout.addStretch()
        actions_layout.addWidget(self.status_label, 1)
        actions_layout.addWidget(self.update_data_button)
        actions_layout.addWidget(self.update_type_button)
        actions_layout.addWidget(self.register_button)
        actions_layout.addWidget(self.generate_button)
        self.layout.addLayout(actions_layout)

    def _create_manga_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.manga_list_widget = QListWidget()
        self.manga_list_widget.setViewMode(QListView.ViewMode.ListMode)
        self.manga_list_widget.setIconSize(QSize(80, 110))
        self.manga_list_widget.setSpacing(5)
        self.manga_list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.manga_list_widget.itemDoubleClicked.connect(self.show_manga_details)
        layout.addWidget(self.manga_list_widget)
        return widget
    
    def _create_log_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_handler.new_log_record.connect(self.append_log_message)
        layout.addWidget(self.log_box)
        return widget
        
    @Slot(str)
    def append_log_message(self, message):
        self.log_box.appendPlainText(message)
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)

    def _create_menu(self):
        menu_bar = self.menuBar()
        tools_menu = menu_bar.addMenu("Ferramentas")
        reload_action = QAction("Recarregar Providers", self)
        reload_action.triggered.connect(self.reload_providers)
        tools_menu.addAction(reload_action)
        import_action = QAction("Importar Novo Provider de Obra...", self)
        import_action.triggered.connect(self.import_new_provider)
        tools_menu.addAction(import_action)

    def _init_backend(self):
        try:
            self.api_client = ApiClient()
            self.s3_handler = ContaboS3Handler(config_path=self.settings_manager.get("s3_config_path"))
            user, pwd = CredentialsManager().load_credentials()
            if not (user and pwd and self.api_client.login(user, pwd)[0]):
                raise Exception("Falha na autenticação. Verifique as credenciais.")
            success, categories = self.api_client.get_all_categories_paginated()
            if not success:
                raise Exception("Falha ao buscar categorias da API.")
            self.category_map = {c['name'].lower(): c['id'] for c in categories}
            logger.info("Backend inicializado com sucesso.")
        except Exception as e:
            logger.error(f"Erro de inicialização do backend: {e}", exc_info=True)
            QMessageBox.critical(self, "Erro de Inicialização", str(e))
            QTimer.singleShot(100, self.close)
            
    def reload_providers(self):
        logger.info("Recarregando providers de obra...")
        self.obra_providers = discover_obra_providers()
        self.chapter_providers = discover_chapter_providers()
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self.provider_combo.addItems(sorted(self.obra_providers.keys()))
        self.provider_combo.blockSignals(False)
        self.on_provider_changed()

    @Slot()
    def on_provider_changed(self):
        provider_name = self.provider_combo.currentText()
        if not provider_name:
            return
        all_filters = self.settings_manager.get("nsfw_filters_by_provider", {})
        provider_filter = all_filters.get(provider_name, "adulto, nsfw, smut, 18+, yaoi")
        self.nsfw_filter_input.setText(provider_filter)

    @Slot()
    def save_nsfw_filter(self):
        provider_name = self.provider_combo.currentText()
        if not provider_name:
            return
        all_filters = self.settings_manager.get("nsfw_filters_by_provider", {})
        all_filters[provider_name] = self.nsfw_filter_input.text().strip()
        self.settings_manager.set("nsfw_filters_by_provider", all_filters)

    def import_new_provider(self):
        providers_path = base_path / "providers_obra"
        file_path, _ = QFileDialog.getOpenFileName(self, "Selecionar Arquivo de Provider", "", "Python Files (*.py)")
        if not file_path:
            return
        source_file = Path(file_path)
        destination_file = providers_path / source_file.name
        if destination_file.exists():
            reply = QMessageBox.question(self, "Substituir?", f"O provider '{source_file.name}' já existe. Deseja substituí-lo?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return
        try:
            shutil.copy(source_file, destination_file)
            QMessageBox.information(self, "Sucesso", f"Provider '{source_file.name}' importado. A lista será recarregada.")
            self.reload_providers()
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível copiar o arquivo: {e}")

    def start_list_scraping(self):
        url = self.url_input.text().strip()
        provider_name = self.provider_combo.currentText()
        if not url or not provider_name:
            QMessageBox.warning(self, "Dados Incompletos", "Por favor, selecione um provider e insira uma URL.")
            return
        self.manga_list_widget.clear()
        self.mangas.clear()
        self.check_queue.clear()
        self.set_ui_enabled(False, "Buscando lista de obras...")
        worker = MangaListScraper(provider_name, self.obra_providers, url)
        worker.signals.progress.connect(self.status_label.setText)
        worker.signals.manga_found.connect(self.on_manga_found)
        worker.signals.finished.connect(self.on_list_finished)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def start_single_scraping(self):
        url = self.url_input.text().strip()
        provider_name = self.provider_combo.currentText()
        if not url or not provider_name:
            QMessageBox.warning(self, "Dados Incompletos", "Por favor, selecione um provider e insira uma URL.")
            return
        ProviderClass = self.obra_providers.get(provider_name)
        if not ProviderClass or not hasattr(ProviderClass, 'get_single_manga_data'):
            QMessageBox.warning(self, "Não Suportado", "O provider selecionado não suporta a busca de obra única.")
            return
        self.manga_list_widget.clear()
        self.mangas.clear()
        self.check_queue.clear()
        self.set_ui_enabled(False, f"Buscando obra única: {url}...")
        
        class SingleScraper(QRunnable):
            def __init__(self, provider_name, provider_classes, url):
                super().__init__()
                self.signals = WorkerSignals()
                self.provider_name = provider_name
                self.provider_classes = provider_classes
                self.url = url
            def run(self):
                try:
                    ProviderClass = self.provider_classes.get(self.provider_name)
                    if not ProviderClass:
                        raise ValueError(f"Provider '{self.provider_name}' não encontrado.")
                    provider = ProviderClass()
                    manga_list = provider.get_single_manga_data(self.url)
                    if not manga_list:
                        self.signals.error.emit("single_scraper", "Nenhuma obra encontrada na URL.")
                        return
                    for manga_data in manga_list:
                        manga = MangaImportData(title=manga_data['title'], source_url=manga_data['url'], cover_url=manga_data.get('cover_url', ''), cover_data=manga_data.get('cover_data'))
                        self.signals.manga_found.emit(manga)
                    self.signals.finished.emit("single_scraper", {})
                except Exception as e:
                    logger.error(f"Erro no SingleScraper: {e}", exc_info=True)
                    self.signals.error.emit("single_scraper", str(e))

        worker = SingleScraper(provider_name, self.obra_providers, url)
        worker.signals.manga_found.connect(self.on_manga_found)
        worker.signals.finished.connect(self.on_list_finished)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    @Slot(str, str)
    def on_worker_error(self, task_type, error_msg):
        logger.error(f"Erro recebido da tarefa '{task_type}': {error_msg}")
        QMessageBox.critical(self, "Erro na Tarefa", f"Ocorreu um erro durante a execução:\n\n{error_msg}")
        self.set_ui_enabled(True, f"Erro: {error_msg}")

    @Slot(str, dict)
    def on_list_finished(self, task_type, data):
        if task_type in ["list_scraper", "single_scraper"]:
            if self.check_queue:
                status_msg = f"{len(self.check_queue)} obra(s) encontrada(s). Verificando existência..."
                self.status_label.setText(status_msg)
                logger.info(status_msg)
                self.process_check_queue()
            else:
                self.set_ui_enabled(True, "Nenhuma obra encontrada.")
                logger.info("Busca concluída, nenhuma obra encontrada.")
    
    @Slot(object)
    def on_manga_found(self, manga_data: MangaImportData):
        if manga_data.source_url in self.mangas:
            return
        item = QListWidgetItem(manga_data.title)
        item.setFont(QFont("Segoe UI", 12))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        item.setBackground(QColor("#A9A9A9"))
        item.setSizeHint(QSize(200, 120))
        item.setData(Qt.ItemDataRole.UserRole, manga_data.source_url)
        self.manga_list_widget.addItem(item)
        manga_data.list_item = item
        self.mangas[manga_data.source_url] = manga_data
        self.check_queue.append(manga_data)

    def process_check_queue(self):
        MAX_CONCURRENT_CHECKS = 5
        active_checks = sum(1 for m in self.mangas.values() if m.list_item.background().color() == QColor("orange"))
        while active_checks < MAX_CONCURRENT_CHECKS and self.check_queue:
            active_checks += 1
            manga_data = self.check_queue.popleft()
            manga_data.list_item.setBackground(QColor("orange"))
            existence_worker = MangaExistenceChecker(self.api_client, manga_data)
            existence_worker.signals.existence_checked.connect(self.on_existence_checked)
            self.thread_pool.start(existence_worker)

    @Slot(str, object)
    def on_existence_checked(self, url, api_data):
        manga = self.mangas.get(url)
        if not manga:
            self.process_check_queue()
            return
        if api_data:
            manga.status = "Já Existente"
            manga.api_response = api_data
            manga.list_item.setBackground(QColor("#005A9E"))
            manga.list_item.setToolTip(f"Já existe no site (ID: {api_data['id']}).")
        else:
            manga.status = "Pendente de Cadastro"
            manga.list_item.setBackground(QColor("#444444"))
            manga.list_item.setToolTip("Não encontrado no site. Pronto para cadastrar.")
        self.process_check_queue()
        active_checks = sum(1 for m in self.mangas.values() if m.list_item.background().color() == QColor("orange"))
        if active_checks == 0 and not self.check_queue:
            status_msg = "Verificação concluída."
            self.set_ui_enabled(True, status_msg)
            logger.info(status_msg)

    @Slot(str)
    def filter_list(self, text):
        for item_data in self.mangas.values():
            item_data.list_item.setHidden(text.lower() not in item_data.title.lower())

    def toggle_selection(self, select: bool):
        state = Qt.CheckState.Checked if select else Qt.CheckState.Unchecked
        for i in range(self.manga_list_widget.count()):
            item = self.manga_list_widget.item(i)
            if not item.isHidden():
                item.setCheckState(state)
            
    def start_registration(self):
        selected_mangas = [m for m in self.mangas.values() if m.list_item.checkState() == Qt.CheckState.Checked and m.status == "Pendente de Cadastro"]
        if not selected_mangas:
            QMessageBox.warning(self, "Nenhuma Obra", "Selecione obras com status 'Pendente de Cadastro'.")
            return
        work_types = ["manga", "manhwa", "manhua", "webtoon", "other"]
        work_type, ok = QInputDialog.getItem(self, "Tipo de Obra", "Selecione o tipo para as novas obras:", work_types, 0, False)
        if not ok or not work_type:
            return
        custom_work_type = ""
        if work_type == "other":
            custom_work_type, ok = QInputDialog.getText(self, "Tipo Personalizado", "Digite o tipo personalizado:", text="Comic")
            if not ok or not custom_work_type:
                return
        self.run_tasks(selected_mangas, "registration", (work_type, custom_work_type))

    def run_tasks(self, mangas: List[MangaImportData], task_type: str, task_args: tuple):
        self.set_ui_enabled(False)
        task_map = {"registration": "Cadastrando", "update": "Atualizando tipo", "full_update": "Atualizando dados"}
        task_description = task_map.get(task_type, "Processando")
        self.progress_dialog = QProgressDialog(f"{task_description} {len(mangas)} obra(s)...", "Cancelar", 0, len(mangas), self)
        self.progress_dialog.canceled.connect(self.cancel_tasks)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.show()
        self.task_queue = deque([(manga, task_type, task_args) for manga in mangas])
        self.tasks_completed = 0
        self.tasks_to_run = len(mangas)
        self.process_next_task_in_queue()

    def process_next_task_in_queue(self):
        if (self.progress_dialog and self.progress_dialog.wasCanceled()) or not self.task_queue:
            self.on_task_finished_base()
            return
        manga, task_type, task_args = self.task_queue.popleft()
        self.status_label.setText(f"Processando: {manga.title}...")
        manga.list_item.setBackground(QColor("orange"))
        nsfw_keywords = {kw.strip() for kw in self.nsfw_filter_input.text().strip().lower().split(',') if kw.strip()}
        provider_name = self.provider_combo.currentText()
        worker = FullProcessWorker(provider_name, self.obra_providers, manga, self.api_client, self.s3_handler, self.category_map, task_type, task_args, nsfw_keywords)
        worker.signals.finished.connect(self.on_task_success_and_continue)
        worker.signals.error.connect(self.on_task_error_and_continue)
        self.thread_pool.start(worker)

    def cancel_tasks(self):
        self.task_queue.clear()
        self.status_label.setText("Cancelando após a tarefa atual...")
        
    def on_task_finished_base(self):
        self.tasks_completed += 1
        if self.progress_dialog:
            self.progress_dialog.setValue(self.tasks_completed)
        if self.tasks_completed >= self.tasks_to_run:
            status_msg = "Processo concluído."
            self.set_ui_enabled(True, status_msg)
            logger.info(status_msg)
            if self.progress_dialog:
                self.progress_dialog.close()
                self.progress_dialog = None
            self.task_queue.clear()

    @Slot(str, dict)
    def on_task_success_and_continue(self, url, api_response):
        self.on_task_success(url, api_response) 
        self.process_next_task_in_queue()

    @Slot(str, str)
    def on_task_error_and_continue(self, url, error_msg):
        self.on_task_error(url, error_msg)
        self.process_next_task_in_queue()

    @Slot(str, dict)
    def on_task_success(self, url, api_response):
        manga = self.mangas.get(url)
        if not manga:
            return
        manga.api_response = api_response
        dialog_text = self.progress_dialog.labelText() if self.progress_dialog else ""
        if "Cadastrando" in dialog_text:
            manga.status = "Cadastrado (Pronto para Monitorar)"
            new_color, tooltip_action = QColor("darkgreen"), "Cadastrado"
        elif "Atualizando dados" in dialog_text:
            manga.status = "Dados Atualizados"
            new_color, tooltip_action = QColor("#9400D3"), "Dados Atualizados"
        else:
            manga.status = "Tipo Atualizado"
            new_color, tooltip_action = QColor("darkcyan"), "Tipo Atualizado"
        manga.list_item.setBackground(new_color)
        display_type = api_response.get('custom_work_type') or api_response.get('work_type')
        manga.list_item.setToolTip(f"{tooltip_action}! Tipo: '{display_type}'. ID: {api_response.get('id')}")
        manga.list_item.setCheckState(Qt.CheckState.Unchecked)
        self.on_task_finished_base()
        
    @Slot(str, str)
    def on_task_error(self, url, error_msg):
        manga = self.mangas.get(url)
        if manga:
            manga.status = "Erro"
            manga.list_item.setBackground(QColor("darkred"))
            manga.list_item.setToolTip(f"Erro: {error_msg}")
        self.on_task_finished_base()

    def start_type_update(self):
        selected_mangas = [m for m in self.mangas.values() if m.list_item.checkState() == Qt.CheckState.Checked and m.api_response.get('id')]
        if not selected_mangas:
            QMessageBox.warning(self, "Nenhuma Obra Válida", "Selecione obras já cadastradas.")
            return
        work_types = ["manga", "manhwa", "manhua", "webtoon", "other"]
        new_type, ok = QInputDialog.getItem(self, "Atualizar Tipo", "Selecione o novo tipo:", work_types, 0, False)
        if not ok or not new_type:
            return
        new_custom_type = ""
        if new_type == "other":
            new_custom_type, ok = QInputDialog.getText(self, "Tipo Personalizado", "Digite o tipo:", text="Comic")
            if not ok:
                return
        self.run_tasks(selected_mangas, "update", (new_type, new_custom_type))
        
    def start_full_update(self):
        selected_mangas = [m for m in self.mangas.values() if m.list_item.checkState() == Qt.CheckState.Checked and m.api_response.get('id')]
        if not selected_mangas:
            QMessageBox.warning(self, "Nenhuma Obra Válida", "Selecione obras já cadastradas.")
            return
        reply = QMessageBox.question(self, "Confirmar Atualização", f"Sobrescrever os dados de {len(selected_mangas)} obra(s) com as informações da fonte?\n\nIsso inclui sinopse, autor, artista, status, gêneros e a imagem da capa.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No:
            return
        self.run_tasks(selected_mangas, "full_update", task_args=())

    @Slot(QListWidgetItem)
    def show_manga_details(self, item: QListWidgetItem):
        manga = self.mangas.get(item.data(Qt.ItemDataRole.UserRole))
        if not manga:
            return
        if manga.details:
            MangaDetailsDialog(manga, self).exec()
            return
        provider_name = self.provider_combo.currentText()
        if not provider_name:
            return
        class DetailsFetcher(QRunnable):
            def __init__(self, provider_name, provider_classes, manga_obj):
                super().__init__()
                self.signals = WorkerSignals()
                self.provider_name = provider_name
                self.provider_classes = provider_classes
                self.manga = manga_obj
            def run(self):
                try:
                    ProviderClass = self.provider_classes.get(self.provider_name)
                    if not ProviderClass:
                        raise ValueError(f"Provider '{self.provider_name}' não encontrado.")
                    provider = ProviderClass()
                    details = provider.get_manga_details(self.manga.source_url)
                    self.signals.finished.emit(self.manga.source_url, details)
                except Exception as e:
                    self.signals.error.emit(self.manga.source_url, str(e))
        worker = DetailsFetcher(provider_name, self.obra_providers, manga)
        
        @Slot(str, dict)
        def on_details_ready(u, d):
            manga.details = d
            MangaDetailsDialog(manga, self).exec()
        worker.signals.finished.connect(on_details_ready)
        worker.signals.error.connect(lambda _, msg: QMessageBox.critical(self, "Erro", f"Não foi possível buscar detalhes: {msg}"))
        self.thread_pool.start(worker)

    def generate_monitoring_file(self):
        selected_mangas = [m for m in self.mangas.values() if m.list_item.checkState() == Qt.CheckState.Checked and m.status == "Cadastrado (Pronto para Monitorar)"]
        if not selected_mangas:
            QMessageBox.warning(self, "Nenhuma Obra Válida", "Selecione obras que foram cadastradas com sucesso (marcadas em verde) para gerar o monitoramento.")
            return
        suggested_bot_name = urlparse(selected_mangas[0].source_url).netloc.split('.')[0].replace('-', '_')
        bot_name, ok = QInputDialog.getText(self, "Nome do Worker", "Digite um nome para o worker (bot):", text=f"bot_{suggested_bot_name}")
        if not ok or not bot_name:
            return
        chapter_provider, ok = QInputDialog.getItem(self, "Provider de Capítulo", "Selecione o provider de capítulo:", self.chapter_providers, 0, False)
        if not ok or not chapter_provider:
            return
        monitoring_config = {bot_name: {}}
        for manga in selected_mangas:
            manga_id = manga.api_response.get('id')
            if manga_id:
                key = f"provider_{chapter_provider}_{manga_id}_{int(time.time())}"
                monitoring_config[bot_name][key] = {"provider": chapter_provider, "manga_url": manga.source_url, "manga_id": manga_id}
        save_path, _ = QFileDialog.getSaveFileName(self, "Salvar Arquivo", "monitoramento_gerado.json", "JSON (*.json)")
        if not save_path:
            return
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(monitoring_config, f, indent=4)
            QMessageBox.information(self, "Sucesso", f"Arquivo salvo em:\n{save_path}\n\nCopie o conteúdo para seu `monitoramento_bots.json`.")
        except Exception as e:
            QMessageBox.critical(self, "Erro ao Salvar", str(e))
    
    def set_ui_enabled(self, enabled, status_text=""):
        for button in [self.fetch_button, self.fetch_single_button, self.register_button, self.generate_button, self.select_all_button, self.unselect_all_button, self.update_type_button, self.update_data_button]:
            button.setEnabled(enabled)
        self.status_label.setText(status_text or ("Pronto." if enabled else "Processando..."))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    AppTheme.apply(app)
    app.setStyleSheet(app.styleSheet() + """
        QListView::item { border: 1px solid #555; border-radius: 5px; padding: 5px; background-color: #333; color: white; }
        QListView::item:selected { background-color: #0078d7; }
    """)
    window = MassImporterWindow()
    window.show()
    sys.exit(app.exec())