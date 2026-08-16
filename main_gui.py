# --- START OF FILE main_gui.py ---

import sys
import os
import multiprocessing
from pathlib import Path

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
import logging
import time
import shutil
from collections import deque
from datetime import datetime
from functools import partial

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

from core.api_client import ApiClient
from core.credentials import CredentialsManager
from core.s3_handler import ContaboS3Handler
from core.processing import validate_and_truncate, natural_sort_key
from core.settings_manager import SettingsManager
from core.stats_manager import StatsManager
from core.workers import (
    ChapterUploadTask, ChapterVerificationWorker, MangaDownloadWorker,
    MangaListFetcher, LoginWorker, ChapterDetailsFetcher, CacheMangaDetailsWorker,
    CategoryFetcher, MangaUpdateWorker, CoverUploadWorker,
    ChapterExistenceVerifier, TaskEnqueuingWorker, ChapterDeleteWorker
)
from ui.theme import AppTheme
from ui.icons import get_icon

try:
    import pyqtgraph as pg
    pg.setConfigOption('background', AppTheme.SECONDARY_COLOR)
    pg.setConfigOption('foreground', AppTheme.MUTED_TEXT_COLOR)
except ImportError:
    pg = None

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel,
    QStackedWidget, QMessageBox, QFrame, QTabWidget, QDialog, QDialogButtonBox, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QStyle, QStatusBar, QSpinBox, QMenu, QFileDialog, QListWidget,
    QListWidgetItem, QAbstractItemView, QFormLayout, QComboBox, QProgressBar, QPlainTextEdit,
    QInputDialog, QSystemTrayIcon, QCheckBox, QDateTimeEdit, QTimeEdit
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QTimer, QRunnable, QThreadPool, Slot, QProcess, QDateTime, QTime,
    QPropertyAnimation, QEasingCurve, QSize, QUrl
)
from PySide6.QtGui import (
    QPalette, QColor, QFont, QIcon, QPixmap, QMovie, QTextCursor, QAction, QDrag, QPainter,
    QDesktopServices
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s', handlers=[logging.FileHandler("uploader.log", encoding='utf-8')])

def discover_providers():
    # usa o registry, que cobre .py (fonte) E .pyd (compilado, via manifest.json);
    # o glob antigo por "*_provider.py" achava zero no pacote congelado.
    try:
        from core.provider_registry import listar_providers
        return sorted(p.nome for p in listar_providers())
    except Exception:
        logging.error("Falha ao listar providers para o combo de monitoramento", exc_info=True)
        return []

class QtLogHandler(logging.Handler, QObject):
    new_log_record = Signal(str)
    def __init__(self):
        super().__init__()
        QObject.__init__(self)
    def emit(self, record):
        msg = self.format(record)
        self.new_log_record.emit(msg)

class StatusServer(QObject):
    message_received = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.socket_name = "NexusUploaderSocket"
        
        QLocalServer.removeServer(self.socket_name)

    def start(self):
        if not self.server.listen(self.socket_name):
            logging.error(f"[GUI Server] Não foi possível iniciar o servidor no socket '{self.socket_name}': {self.server.errorString()}")
            if self.server.serverError() == QLocalServer.AddressInUseError:
                logging.warning("[GUI Server] O nome do socket já está em uso. Tentando remover e reiniciar...")
                QLocalServer.removeServer(self.socket_name)
                if not self.server.listen(self.socket_name):
                     logging.error("[GUI Server] Tentativa de reinício falhou.")
                     return
            else:
                return
        
        self.server.newConnection.connect(self.handle_connection)
        logging.info(f"[GUI Server] Servidor local iniciado e ouvindo no socket '{self.socket_name}'")

    def stop(self):
        self.server.close()
        logging.info("[GUI Server] Servidor local parado.")

    @Slot()
    def handle_connection(self):
        socket = self.server.nextPendingConnection()
        if socket:
            socket.readyRead.connect(lambda: self.read_data(socket))

    def read_data(self, socket):
        data = socket.readAll().data()
        if data:
            try:
                self.message_received.emit(json.loads(data.decode('utf-8')))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logging.warning(f"[GUI Server] Mensagem malformada recebida: {data}")
        socket.disconnectFromServer()

class LoginWidget(QWidget):
    login_requested = Signal(str, str)
    def __init__(self, credentials_manager, parent=None):
        super().__init__(parent)
        self.credentials_manager = credentials_manager
        self._setup_ui()
        self.load_credentials()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(50, 50, 50, 50)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self._create_login_form_widget())
        self.view_stack.addWidget(self._create_loading_widget())
        main_layout.addWidget(self.view_stack)

    def _create_login_form_widget(self):
        master_container = QWidget()
        container_layout = QVBoxLayout(master_container)
        container_layout.setSpacing(20)
        container_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        form_frame = QFrame()
        form_frame.setFixedWidth(350)
        form_layout = QVBoxLayout(form_frame)
        form_layout.setSpacing(15)

        logo_label = self._create_logo_from_file()
        title_label = QLabel("Hybrid Uploader")
        title_label.setObjectName("TitleLabel")
        container_layout.addWidget(logo_label, alignment=Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(title_label, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Usuário")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Senha")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self._on_login_click)
        form_layout.addWidget(self.username_input)
        form_layout.addWidget(self.password_input)
        
        login_button = QPushButton("Entrar")
        login_button.setObjectName("PrimaryButton")
        login_button.setFixedHeight(40)
        login_button.clicked.connect(self._on_login_click)
        form_layout.addWidget(login_button)
        container_layout.addWidget(form_frame)
        return master_container

    def load_credentials(self):
        username, password = self.credentials_manager.load_credentials()
        if username and password:
            self.username_input.setText(username)
            self.password_input.setText(password)
            self.password_input.setFocus()

    def _on_login_click(self):
        username, password = self.username_input.text(), self.password_input.text()
        if username and password:
            self.show_loading()
            self.login_requested.emit(username, password)
        else:
            QMessageBox.warning(self, "Campos Vazios", "Por favor, preencha o usuário e a senha.")

    def _create_logo_from_file(self):
        logo_path = base_path / "assets" / "logo.png"
        logo_label = QLabel()
        if os.path.exists(logo_path):
            logo_label.setPixmap(QPixmap(str(logo_path)).scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        return logo_label

    def _create_loading_widget(self):
        loading_widget = QWidget()
        layout = QVBoxLayout(loading_widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gif_path = base_path / "assets" / "loading.gif"
        self.loading_label = QLabel()
        if os.path.exists(gif_path):
            self.movie = QMovie(str(gif_path))
            self.movie.setScaledSize(QSize(64,64))
            self.loading_label.setMovie(self.movie)
        else:
            self.loading_label.setText("Carregando...")
        layout.addWidget(self.loading_label)
        return loading_widget

    def show_loading(self):
        if hasattr(self, 'movie'):
            self.movie.start()
        self.view_stack.setCurrentIndex(1)

    def show_form(self):
        if hasattr(self, 'movie'):
            self.movie.stop()
        self.view_stack.setCurrentIndex(0)

class AddMonitorDialog(QDialog):
    def __init__(self, parent, api_client, bot_ids=None, current_bot_id=None):
        super().__init__(parent)
        self.api_client = api_client
        self.selected_manga_data = None
        self.setWindowTitle("Adicionar/Editar Obra Monitorada")
        self.setMinimumSize(550, 600)
        
        main_layout = QVBoxLayout(self)

        bot_selection_layout = QFormLayout()
        self.bot_selector_combo = QComboBox()
        if bot_ids:
            self.bot_selector_combo.addItems(sorted(bot_ids))
        if current_bot_id:
            self.bot_selector_combo.setCurrentText(current_bot_id)
        bot_selection_layout.addRow("Alocar no Worker:", self.bot_selector_combo)
        main_layout.addLayout(bot_selection_layout)

        form_layout = QFormLayout()
        self.monitor_type_combo = QComboBox()
        self.monitor_type_combo.addItems(["Pasta Local", "Provider (URL)"])
        form_layout.addRow("Tipo de Monitoramento:", self.monitor_type_combo)
        main_layout.addLayout(form_layout)

        self.stacked_widget = QStackedWidget()
        self.folder_monitor_widget = self._create_folder_monitor_widget()
        self.provider_monitor_widget = self._create_provider_monitor_widget()
        self.stacked_widget.addWidget(self.folder_monitor_widget)
        self.stacked_widget.addWidget(self.provider_monitor_widget)
        main_layout.addWidget(self.stacked_widget)

        self.monitor_type_combo.currentIndexChanged.connect(self.stacked_widget.setCurrentIndex)
        self.monitor_type_combo.currentIndexChanged.connect(self.check_form_completeness)

        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Digite o nome da obra no seu site para buscar")
        search_button = QPushButton("Buscar")
        search_button.clicked.connect(self.search_manga)
        self.search_input.returnPressed.connect(self.search_manga)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(search_button)
        main_layout.addLayout(search_layout)

        self.results_list = QListWidget()
        self.results_list.itemSelectionChanged.connect(self.on_manga_selected)
        main_layout.addWidget(QLabel("Obra de Destino (no seu site):"))
        main_layout.addWidget(self.results_list)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        main_layout.addWidget(self.button_box)
        
        self.check_form_completeness()

    def _create_folder_monitor_widget(self):
        widget = QWidget()
        layout = QFormLayout(widget)
        layout.setContentsMargins(0, 10, 0, 0)
        
        folder_layout = QHBoxLayout()
        self.folder_path_input = QLineEdit()
        self.folder_path_input.setPlaceholderText("Selecione a pasta a ser monitorada")
        self.folder_path_input.setReadOnly(True)
        browse_button = QPushButton("Procurar...")
        browse_button.clicked.connect(self.select_folder)
        folder_layout.addWidget(self.folder_path_input)
        folder_layout.addWidget(browse_button)
        layout.addRow("Pasta:", folder_layout)

        self.wait_time_spinbox = QSpinBox()
        self.wait_time_spinbox.setRange(1, 1440)
        self.wait_time_spinbox.setValue(3)
        self.wait_time_spinbox.setSuffix(" minutos (estabilização)")
        layout.addRow("Tempo de Espera:", self.wait_time_spinbox)
        
        self.folder_path_input.textChanged.connect(self.check_form_completeness)
        return widget

    def _create_provider_monitor_widget(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 10, 0, 0)
        form_layout = QFormLayout()

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(discover_providers())
        form_layout.addRow("Provider:", self.provider_combo)

        self.manga_url_input = QLineEdit()
        self.manga_url_input.setPlaceholderText("https://exemplo.com/manga/nome-da-obra")
        form_layout.addRow("URL da Obra:", self.manga_url_input)
        layout.addLayout(form_layout)

        schedule_group = QFrame()
        schedule_group.setFrameShape(QFrame.Shape.StyledPanel)
        schedule_layout = QVBoxLayout(schedule_group)
        
        schedule_layout.addWidget(QLabel("<b>Configuração de Agendamento</b>"))

        self.schedule_mode_combo = QComboBox()
        self.schedule_mode_combo.addItems(["A cada X Horas (Intervalo)", "Dias da Semana Específicos"])
        schedule_layout.addWidget(self.schedule_mode_combo)

        self.schedule_stacked_widget = QStackedWidget()
        
        interval_panel = QWidget()
        interval_layout = QFormLayout(interval_panel)
        self.check_interval_spinbox = QSpinBox()
        self.check_interval_spinbox.setRange(1, 168)
        self.check_interval_spinbox.setValue(6)
        self.check_interval_spinbox.setSuffix(" horas")
        interval_layout.addRow("Verificar a cada:", self.check_interval_spinbox)
        self.schedule_stacked_widget.addWidget(interval_panel)

        weekly_panel = QWidget()
        weekly_layout = QVBoxLayout(weekly_panel)
        self.day_checkboxes = {}
        days_layout = QHBoxLayout()
        days = {"Seg": "monday", "Ter": "tuesday", "Qua": "wednesday", "Qui": "thursday", "Sex": "friday", "Sáb": "saturday", "Dom": "sunday"}
        for day_name, day_id in days.items():
            checkbox = QCheckBox(day_name)
            self.day_checkboxes[day_id] = checkbox
            days_layout.addWidget(checkbox)
        weekly_layout.addLayout(days_layout)
        
        time_layout = QFormLayout()
        self.schedule_time_edit = QTimeEdit()
        self.schedule_time_edit.setDisplayFormat("HH:mm")
        self.schedule_time_edit.setTime(QTime(18, 0))
        time_layout.addRow("Horário da Verificação:", self.schedule_time_edit)
        weekly_layout.addLayout(time_layout)
        self.schedule_stacked_widget.addWidget(weekly_panel)

        self.schedule_mode_combo.currentIndexChanged.connect(self.schedule_stacked_widget.setCurrentIndex)
        
        schedule_layout.addWidget(self.schedule_stacked_widget)
        layout.addWidget(schedule_group)
        
        self.manga_url_input.textChanged.connect(self.check_form_completeness)
        return widget

    def set_data(self, data_for_edit, manga_data):
        config_data = data_for_edit.get('config', {})
        is_provider = 'provider' in config_data
        
        if is_provider:
            self.monitor_type_combo.setCurrentIndex(1)
            self.provider_combo.setCurrentText(config_data.get('provider', ''))
            self.manga_url_input.setText(config_data.get('manga_url', ''))
            
            schedule_mode = config_data.get('schedule_mode', 'interval')
            if schedule_mode == 'weekly':
                self.schedule_mode_combo.setCurrentIndex(1)
                scheduled_days = config_data.get('schedule_days', [])
                for day_id, checkbox in self.day_checkboxes.items():
                    checkbox.setChecked(day_id in scheduled_days)
                time_str = config_data.get('schedule_time', '18:00')
                self.schedule_time_edit.setTime(QTime.fromString(time_str, "HH:mm"))
            else:
                self.schedule_mode_combo.setCurrentIndex(0)
                self.check_interval_spinbox.setValue(config_data.get('check_interval_hours', 6))
        else:
            self.monitor_type_combo.setCurrentIndex(0)
            self.folder_path_input.setText(data_for_edit.get('config_key', ''))
            self.wait_time_spinbox.setValue(config_data.get('wait_time_seconds', 180) // 60)

        self.selected_manga_data = manga_data
        if manga_data:
            item = QListWidgetItem(manga_data.get('title', 'ID Desconhecido'))
            item.setData(Qt.ItemDataRole.UserRole, manga_data)
            self.results_list.addItem(item)
            self.results_list.setCurrentItem(item)
        
        self.check_form_completeness()

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecione a Pasta")
        if folder: self.folder_path_input.setText(folder)

    def search_manga(self):
        query = self.search_input.text().strip()
        if not query: return
        self.results_list.clear()
        self.results_list.addItem("Buscando...")
        self.search_input.setEnabled(False) 

        worker = MangaListFetcher(self.api_client, query)
        worker.signals.finished.connect(self._on_search_finished)
        worker.signals.error.connect(self._on_search_error)
        self.parent().thread_pool.start(worker)

    @Slot(str, dict)
    def _on_search_finished(self, task_id, payload):
        self.results_list.clear()
        self.search_input.setEnabled(True) 

        current_selection_id = self.selected_manga_data['id'] if self.selected_manga_data else None
        new_item_to_select = None
        
        for manga in payload.get('results', []):
            item = QListWidgetItem(manga['title'])
            item.setData(Qt.ItemDataRole.UserRole, manga)
            self.results_list.addItem(item)
            if manga['id'] == current_selection_id:
                new_item_to_select = item
        
        if new_item_to_select:
            self.results_list.setCurrentItem(new_item_to_select)

    @Slot(str, dict)
    def _on_search_error(self, task_id, payload):
        self.results_list.clear()
        self.search_input.setEnabled(True) 
        self.results_list.addItem("Erro ao buscar.")
        error_message = payload.get('message', 'Erro desconhecido.')
        QMessageBox.warning(self, "Erro de Busca", error_message)

    def on_manga_selected(self):
        if self.results_list.selectedItems():
            self.selected_manga_data = self.results_list.selectedItems()[0].data(Qt.ItemDataRole.UserRole)
        else:
            self.selected_manga_data = None
        self.check_form_completeness()

    def check_form_completeness(self):
        is_ok_enabled = False
        has_manga_selected = bool(self.selected_manga_data)

        if self.monitor_type_combo.currentIndex() == 0:
            is_ok_enabled = bool(self.folder_path_input.text() and has_manga_selected)
        else:
            is_ok_enabled = bool(self.manga_url_input.text() and self.provider_combo.currentText() and has_manga_selected)
        
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(is_ok_enabled)
        
    def get_results(self):
        results = {
            'manga_data': self.selected_manga_data,
            'target_bot_id': self.bot_selector_combo.currentText()
        }
        if self.monitor_type_combo.currentIndex() == 0:
            results['type'] = 'folder'
            results['path'] = self.folder_path_input.text()
            results['config'] = {"wait_time_seconds": self.wait_time_spinbox.value() * 60}
        else:
            results['type'] = 'provider'
            config = {
                "provider": self.provider_combo.currentText(),
                "manga_url": self.manga_url_input.text(),
            }
            if self.schedule_mode_combo.currentIndex() == 1:
                config['schedule_mode'] = 'weekly'
                selected_days = [day_id for day_id, cb in self.day_checkboxes.items() if cb.isChecked()]
                config['schedule_days'] = selected_days
                config['schedule_time'] = self.schedule_time_edit.time().toString("HH:mm")
            else:
                config['schedule_mode'] = 'interval'
                config['check_interval_hours'] = self.check_interval_spinbox.value()
            results['config'] = config
        return results

class UploadStagingDialog(QDialog):
    def __init__(self, chapters_to_upload, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preparar Upload Manual")
        self.setMinimumSize(850, 600)
        self.chapters = chapters_to_upload
        self.chapter_configs = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{len(self.chapters)} capítulo(s) detectado(s). Configure e selecione."))
        
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabels(["Capítulo", "Número", "Imgs"])
        self.tree_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree_widget.currentItemChanged.connect(self.on_chapter_selected)
        layout.addWidget(self.tree_widget)
        header = self.tree_widget.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        main_edit_area = QHBoxLayout()

        self.edit_area = QFrame()
        self.edit_area.setFrameShape(QFrame.Shape.StyledPanel)
        self.edit_area.setVisible(False)
        edit_layout = QFormLayout(self.edit_area)
        self.edit_label = QLabel("Editando Capítulo: ")
        self.access_combo = QComboBox()
        self.access_combo.addItems(['public', 'vip_timed', 'coins'])
        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("Horas (VIP) ou Moedas")
        self.access_combo.currentTextChanged.connect(self.save_current_config)
        self.value_input.textChanged.connect(self.save_current_config)
        edit_layout.addRow(self.edit_label)
        edit_layout.addRow("Nível de Acesso:", self.access_combo)
        edit_layout.addRow("Preço/Duração:", self.value_input)
        self.bulk_apply_button = QPushButton("✔ Aplicar a Todos os Marcados")
        self.bulk_apply_button.clicked.connect(self._apply_config_to_checked)
        edit_layout.addRow(self.bulk_apply_button)

        self.schedule_area = QFrame()
        self.schedule_area.setFrameShape(QFrame.Shape.StyledPanel)
        schedule_layout = QFormLayout(self.schedule_area)
        schedule_label = QLabel("<b>Agendar Upload</b>")
        self.schedule_checkbox = QCheckBox("Agendar para mais tarde")
        self.schedule_datetime_edit = QDateTimeEdit()
        self.schedule_datetime_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.schedule_datetime_edit.setCalendarPopup(True)
        self.schedule_datetime_edit.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.schedule_datetime_edit.setEnabled(False)
        self.schedule_checkbox.toggled.connect(self.schedule_datetime_edit.setEnabled)
        schedule_layout.addRow(schedule_label)
        schedule_layout.addRow(self.schedule_checkbox)
        schedule_layout.addRow("Data e Hora:", self.schedule_datetime_edit)

        self.overwrite_checkbox = QCheckBox("Substituir capítulos que já existem no site")
        self.overwrite_checkbox.setToolTip(
            "Sem isto, capítulo com número já publicado é ignorado.\n"
            "Com isto, as páginas novas substituem as antigas (as antigas saem do R2).")
        schedule_layout.addRow(self.overwrite_checkbox)

        main_edit_area.addWidget(self.edit_area)
        main_edit_area.addWidget(self.schedule_area)
        layout.addLayout(main_edit_area)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        self.populate_tree()
        if self.tree_widget.topLevelItemCount() > 0:
            self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))

    def populate_tree(self):
        for path, data in self.chapters.items():
            item = QTreeWidgetItem(self.tree_widget)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setText(0, os.path.basename(path))
            item.setText(1, str(data.get('number', 'N/A')))
            item.setText(2, str(len(data['images'])))
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            self.chapter_configs[path] = {'access_level': 'public', 'value': ''}

    def on_chapter_selected(self, current, previous):
        if previous:
            self.save_config_for_item(previous)
        if not current:
            self.edit_area.setVisible(False)
            return
        path = current.data(0, Qt.ItemDataRole.UserRole)
        config = self.chapter_configs.get(path)
        if config:
            self.edit_label.setText(f"<b>Editando:</b> {os.path.basename(path)}")
            self.access_combo.setCurrentText(config['access_level'])
            self.value_input.setText(config['value'])
            self.edit_area.setVisible(True)

    def save_config_for_item(self, item):
        if item:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            self.chapter_configs[path] = {'access_level': self.access_combo.currentText(), 'value': self.value_input.text()}

    def save_current_config(self):
        self.save_config_for_item(self.tree_widget.currentItem())

    def _apply_config_to_checked(self):
        access, value = self.access_combo.currentText(), self.value_input.text()
        checked_items = [self.tree_widget.topLevelItem(i) for i in range(self.tree_widget.topLevelItemCount()) if self.tree_widget.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]
        if not checked_items: return
        if QMessageBox.question(self, "Confirmar", f"Aplicar '{access}' (Valor: '{value}') para os {len(checked_items)} capítulos?") == QMessageBox.StandardButton.Yes:
            for item in checked_items:
                path = item.data(0, Qt.ItemDataRole.UserRole)
                self.chapter_configs[path] = {'access_level': access, 'value': value}

    def get_selected_chapters_config(self):
        self.save_current_config()
        selected = []
        scheduled_time = self.schedule_datetime_edit.dateTime().toPython().isoformat() if self.schedule_checkbox.isChecked() else None
        for i in range(self.tree_widget.topLevelItemCount()):
            item = self.tree_widget.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                path = item.data(0, Qt.ItemDataRole.UserRole)
                orig = self.chapters[path]
                cfg = self.chapter_configs[path]
                task_config = {
                    'task_id': f"manual-{os.path.basename(path)}-{time.time()}", 'path': path, 'images': orig['images'],
                    'number': str(validate_and_truncate(item.text(1), 100)), 'access_level': cfg['access_level'],
                    'vip_duration_hours': cfg['value'] if cfg['access_level'] == 'vip_timed' else None,
                    'coin_cost': cfg['value'] if cfg['access_level'] == 'coins' else None,
                    'scheduled_time': scheduled_time,
                    'overwrite': self.overwrite_checkbox.isChecked()
                }
                selected.append(task_config)
        return selected

class DropArea(QLabel):
    filesDropped = Signal(list)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("\n\nArraste as Pastas dos Capítulos Aqui\n\n")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        self.setObjectName("DropArea")
        self.setStyleSheet(f"#DropArea {{ border: 2px dashed {AppTheme.HOVER_COLOR}; border-radius: 8px; }}")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(f"#DropArea {{ border: 2px solid {AppTheme.ACCENT_COLOR}; border-radius: 8px; background-color: {AppTheme.TERTIARY_COLOR}; }}")

    def dragLeaveEvent(self, event):
        self.setStyleSheet(f"#DropArea {{ border: 2px dashed {AppTheme.HOVER_COLOR}; border-radius: 8px; }}")

    def dropEvent(self, event):
        self.dragLeaveEvent(event)
        self.filesDropped.emit([url.toLocalFile() for url in event.mimeData().urls()])

class LoadingScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(20)
        self.setContentsMargins(50, 50, 50, 50)
        gif_path = base_path / "assets" / "loading.gif"
        loading_label = QLabel()
        if os.path.exists(gif_path):
            self.movie = QMovie(str(gif_path))
            self.movie.setScaledSize(QSize(64, 64))

            loading_label.setMovie(self.movie)
        else:
            loading_label.setText("Carregando...")
        self.status_label = QLabel("Iniciando...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar = QProgressBar()
        layout.addWidget(loading_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

    def start_movie(self):
        if hasattr(self, 'movie'):
            self.movie.start()
    def set_progress(self, value, maximum, text):
        self.progress_bar.setMaximum(maximum)
        self.progress_bar.setValue(value)
        self.status_label.setText(text)

class SettingsDialog(QDialog):
    def __init__(self, settings_manager, credentials_manager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.credentials_manager = credentials_manager
        self.setWindowTitle("Configurações")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        self.works_limit_spinbox = QSpinBox()
        self.works_limit_spinbox.setRange(1, 500)
        self.works_limit_spinbox.setValue(self.settings_manager.get("works_limit_per_bot"))
        form_layout.addRow("Limite de obras por worker:", self.works_limit_spinbox)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        user, pwd = self.credentials_manager.load_credentials()
        self.username_input.setText(user or "")
        self.password_input.setText(pwd or "")
        form_layout.addRow("Usuário do Site:", self.username_input)
        form_layout.addRow("Senha do Site:", self.password_input)
        self.notifications_checkbox = QCheckBox("Ativar notificações de desktop")
        self.notifications_checkbox.setChecked(self.settings_manager.get("enable_notifications"))
        form_layout.addRow(self.notifications_checkbox)
        self.tray_checkbox = QCheckBox("Minimizar para a bandeja ao fechar")
        self.tray_checkbox.setChecked(self.settings_manager.get("minimize_to_tray"))
        form_layout.addRow(self.tray_checkbox)
        layout.addLayout(form_layout)

        # --- Chaves do R2 (Cloudflare) --------------------------------------
        # As chaves ficam num cofre CIFRADO; o app decifra internamente pra usar.
        # Aqui NÃO se exibe/copia o que já está gravado — só dá pra TROCAR.
        r2_sep = QFrame()
        r2_sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(r2_sep)
        r2_titulo = QLabel("Chaves do R2 (armazenamento)")
        r2_titulo.setStyleSheet("font-weight: bold; padding-top: 4px;")
        layout.addWidget(r2_titulo)

        self._s3_config_path = self.settings_manager.get("s3_config_path") or "s3_config.json"
        try:
            from core.s3_handler import s3_config_existe
            _r2_ok = s3_config_existe(self._s3_config_path)
        except Exception:
            _r2_ok = False
        r2_status = QLabel("Status: <b>configurado</b> (chaves ocultas)" if _r2_ok
                           else "Status: <b>não configurado</b>")
        r2_status.setStyleSheet(f"color: {AppTheme.MUTED_TEXT_COLOR}; font-size: 8pt;")
        layout.addWidget(r2_status)
        r2_hint = QLabel("Preencha só para TROCAR as chaves. Deixe em branco para "
                         "manter as atuais. As chaves existentes não são exibidas.")
        r2_hint.setWordWrap(True)
        r2_hint.setStyleSheet(f"color: {AppTheme.MUTED_TEXT_COLOR}; font-size: 8pt;")
        layout.addWidget(r2_hint)

        # placeholder mostra "••••" quando JÁ há chave (feedback de "configurado"
        # sem revelar nada) ou o texto de ajuda quando não há nada gravado
        ph_conf = "•••••••••••• (configurada — em branco mantém)"
        r2_form = QFormLayout()
        self.r2_access_input = QLineEdit()
        self.r2_access_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.r2_access_input.setPlaceholderText(ph_conf if _r2_ok else "nova Access Key")
        self.r2_secret_input = QLineEdit()
        self.r2_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.r2_secret_input.setPlaceholderText(ph_conf if _r2_ok else "nova Secret Key")
        self.r2_endpoint_input = QLineEdit()
        self.r2_endpoint_input.setPlaceholderText("endpoint_url (opcional, só se mudar)")
        self.r2_bucket_input = QLineEdit()
        self.r2_bucket_input.setPlaceholderText("bucket_name (opcional, só se mudar)")
        r2_form.addRow("Access Key:", self.r2_access_input)
        r2_form.addRow("Secret Key:", self.r2_secret_input)
        r2_form.addRow("Endpoint:", self.r2_endpoint_input)
        r2_form.addRow("Bucket:", self.r2_bucket_input)
        layout.addLayout(r2_form)

        # --- Atualização pelo Git -------------------------------------------
        git_sep = QFrame()
        git_sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(git_sep)
        git_titulo = QLabel("Atualização automática (Git)")
        git_titulo.setStyleSheet("font-weight: bold; padding-top: 4px;")
        layout.addWidget(git_titulo)
        git_hint = QLabel("Repositório PRIVADO precisa de um token de acesso (PAT), "
                          "não da senha do GitHub.")
        git_hint.setWordWrap(True)
        git_hint.setStyleSheet(f"color: {AppTheme.MUTED_TEXT_COLOR}; font-size: 8pt;")
        layout.addWidget(git_hint)

        from core.git_updater import GitUpdater
        self.git_updater = GitUpdater()
        gc = self.git_updater.load_config()

        git_form = QFormLayout()
        self.git_repo_input = QLineEdit(gc.get("repo_url", ""))
        self.git_repo_input.setPlaceholderText("https://github.com/usuario/repo.git")
        self.git_email_input = QLineEdit(gc.get("email", ""))
        self.git_email_input.setPlaceholderText("email ou usuário do GitHub")
        self.git_token_input = QLineEdit(gc.get("token", ""))
        self.git_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.git_token_input.setPlaceholderText("Personal Access Token (ghp_...)")
        self.git_branch_input = QLineEdit(gc.get("branch", "main") or "main")
        git_form.addRow("URL do repositório:", self.git_repo_input)
        git_form.addRow("Email/usuário:", self.git_email_input)
        git_form.addRow("Token de acesso:", self.git_token_input)
        git_form.addRow("Branch:", self.git_branch_input)
        layout.addLayout(git_form)

        self.git_auto_checkbox = QCheckBox("Verificar atualização ao abrir o app")
        self.git_auto_checkbox.setChecked(bool(gc.get("auto", True)))
        layout.addWidget(self.git_auto_checkbox)

        git_btn_row = QHBoxLayout()
        self.git_status_label = QLabel("")
        self.git_status_label.setStyleSheet(f"color: {AppTheme.MUTED_TEXT_COLOR}; font-size: 8pt;")
        self.git_check_btn = QPushButton("Verificar agora")
        self.git_check_btn.clicked.connect(self._git_check_now)
        git_btn_row.addWidget(self.git_status_label, 1)
        git_btn_row.addWidget(self.git_check_btn)
        layout.addLayout(git_btn_row)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _salvar_chaves_r2(self):
        """Grava novas chaves R2 no cofre SÓ se access+secret foram preenchidas.
        Não lê/exibe as atuais; mescla endpoint/bucket sobre o que já existe."""
        ak = self.r2_access_input.text().strip()
        sk = self.r2_secret_input.text().strip()
        if not (ak and sk):
            return  # nada a trocar
        from core.s3_handler import carregar_s3_config, salvar_s3_config
        try:
            atual = carregar_s3_config(self._s3_config_path)
        except Exception:
            atual = {}
        atual["access_key"] = ak
        atual["secret_key"] = sk
        ep = self.r2_endpoint_input.text().strip()
        bk = self.r2_bucket_input.text().strip()
        if ep:
            atual["endpoint_url"] = ep
        if bk:
            atual["bucket_name"] = bk
        atual.setdefault("endpoint_url", "")
        atual.setdefault("bucket_name", "")
        atual.setdefault("cdn_base_url", "")
        atual.setdefault("path_prefix", "")
        salvar_s3_config(atual, self._s3_config_path)

    def _salvar_git(self):
        self.git_updater.save_config(
            repo_url=self.git_repo_input.text().strip(),
            email=self.git_email_input.text().strip(),
            token=self.git_token_input.text().strip(),
            branch=self.git_branch_input.text().strip() or "main",
            auto=self.git_auto_checkbox.isChecked(),
        )
        if self.git_repo_input.text().strip():
            try:
                self.git_updater.ensure_remote()
            except Exception:
                pass

    def _git_check_now(self):
        # salva o que está nos campos antes de testar
        self._salvar_git()
        if not self.git_updater.is_configured():
            self.git_status_label.setText("Preencha URL do repositório e token.")
            return
        self.git_check_btn.setEnabled(False)
        self.git_status_label.setText("Verificando...")

        from core.workers import GitUpdateWorker
        worker = GitUpdateWorker(self.git_updater)
        worker.signals.finished.connect(self._git_check_done)
        # thread_pool do app (parent é a MainWindow)
        self.parent().thread_pool.start(worker)

    @Slot(str, dict)
    def _git_check_done(self, _task, payload):
        self.git_check_btn.setEnabled(True)
        status = payload.get("status")
        msg = payload.get("message", "")
        self.git_status_label.setText(msg)
        if status == "atualizado":
            QMessageBox.information(self, "Atualização baixada",
                                    f"{msg}\n\nReinicie o aplicativo para aplicar.")
        elif status == "sem-acesso":
            # abre o modal de verificação; aprovado, tenta o pull de novo
            from ui.email_gate import garantir_acesso
            if garantir_acesso(self):
                self._git_check_now()

    def accept(self):
        self.settings_manager.set("works_limit_per_bot", self.works_limit_spinbox.value())
        self.settings_manager.set("enable_notifications", self.notifications_checkbox.isChecked())
        self.settings_manager.set("minimize_to_tray", self.tray_checkbox.isChecked())
        self.settings_manager.save_settings()
        self.credentials_manager.save_credentials(self.username_input.text(), self.password_input.text())
        try:
            self._salvar_chaves_r2()
        except Exception as e:
            QMessageBox.warning(self, "Chaves R2", f"Não foi possível salvar as chaves R2: {e}")
        self._salvar_git()
        QMessageBox.information(self, "Configurações Salvas", "Algumas configurações podem exigir que o aplicativo seja reiniciado para terem efeito.")
        super().accept()

class CoverDropArea(QLabel):
    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setText("Arraste uma imagem de capa aqui\nou clique em 'Procurar'")
        self.setMinimumHeight(100)
        self.setStyleSheet(f"""
            CoverDropArea {{
                border: 2px dashed {AppTheme.HOVER_COLOR};
                border-radius: 8px;
                color: {AppTheme.MUTED_TEXT_COLOR};
                font-style: italic;
            }}
        """)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
            self.setStyleSheet(f"""
                CoverDropArea {{
                    border: 2px solid {AppTheme.ACCENT_COLOR};
                    background-color: {AppTheme.TERTIARY_COLOR};
                    border-radius: 8px;
                    color: {AppTheme.TEXT_COLOR};
                }}
            """)

    def dragLeaveEvent(self, event):
        self.setStyleSheet(f"""
            CoverDropArea {{
                border: 2px dashed {AppTheme.HOVER_COLOR};
                border-radius: 8px;
                color: {AppTheme.MUTED_TEXT_COLOR};
                font-style: italic;
            }}
        """)

    def dropEvent(self, event):
        self.dragLeaveEvent(event)
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            if url.isLocalFile():
                self.fileDropped.emit(url.toLocalFile())
            else:
                self.fileDropped.emit(url.toString())

class MangaEditDialog(QDialog):
    def __init__(self, parent, all_categories, manga_data=None):
        super().__init__(parent)
        self.manga_data = manga_data
        self.all_categories = all_categories
        self.cover_image_path = None

        self.setWindowTitle("Editar Obra" if manga_data else "Criar Nova Obra")
        self.setMinimumWidth(500)
        
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        
        self.title_input = QLineEdit()
        form_layout.addRow("Título:", self.title_input)
        
        self.alternative_titles_input = QLineEdit()
        form_layout.addRow("Títulos Alternativos:", self.alternative_titles_input)
        
        self.description_input = QPlainTextEdit()
        self.description_input.setPlaceholderText("Sinopse da obra...")
        form_layout.addRow("Descrição:", self.description_input)
        
        self.author_input = QLineEdit()
        form_layout.addRow("Autor(es):", self.author_input)
        
        self.artist_input = QLineEdit()
        form_layout.addRow("Artista(s):", self.artist_input)
        
        self.status_combo = QComboBox()
        self.status_combo.addItems(["ongoing", "completed", "hiatus", "cancelled", "draft"])
        form_layout.addRow("Status:", self.status_combo)
        
        self.work_type_combo = QComboBox()
        self.work_type_combo.addItems(["manga", "manhwa", "manhua", "webtoon", "other"])
        form_layout.addRow("Tipo de Obra:", self.work_type_combo)

        self.custom_work_type_input = QLineEdit()
        self.custom_work_type_input.setPlaceholderText("Especifique o tipo aqui (ex: Novel)")
        self.custom_work_type_label = QLabel("Tipo Personalizado:")
        form_layout.addRow(self.custom_work_type_label, self.custom_work_type_input)
        self.custom_work_type_label.hide()
        self.custom_work_type_input.hide()
        self.work_type_combo.currentTextChanged.connect(self._toggle_custom_type_field)

        self.categories_input = QLineEdit()
        self.categories_input.setPlaceholderText("Ação, Aventura, Comédia (separado por vírgulas)")
        form_layout.addRow("Categorias:", self.categories_input)
        
        cover_layout = QVBoxLayout()
        self.cover_drop_area = CoverDropArea()
        self.cover_drop_area.fileDropped.connect(self._set_cover_from_drop)
        browse_button = QPushButton("Procurar Capa no Computador...")
        browse_button.clicked.connect(self._select_cover_image)
        cover_layout.addWidget(self.cover_drop_area)
        cover_layout.addWidget(browse_button)
        form_layout.addRow("Imagem de Capa:", cover_layout)

        self.is_nsfw_checkbox = QCheckBox("Conteúdo Adulto (+18)?")
        form_layout.addRow(self.is_nsfw_checkbox)
        
        layout.addLayout(form_layout)
        
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.title_input.textChanged.connect(lambda text: self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(text.strip())))
        
        layout.addWidget(self.button_box)

        if self.manga_data:
            self._populate_fields()

    def _toggle_custom_type_field(self, text):
        is_other = text == "other"
        self.custom_work_type_label.setVisible(is_other)
        self.custom_work_type_input.setVisible(is_other)

    def _populate_fields(self):
        self.title_input.setText(self.manga_data.get('title', ''))
        self.alternative_titles_input.setText(self.manga_data.get('alternative_titles', ''))
        self.description_input.setPlainText(self.manga_data.get('description', ''))
        self.author_input.setText(self.manga_data.get('author', ''))
        self.artist_input.setText(self.manga_data.get('artist', ''))
        self.status_combo.setCurrentText(self.manga_data.get('status', 'ongoing'))
        
        work_type = self.manga_data.get('work_type', 'manga')
        self.work_type_combo.setCurrentText(work_type)
        if work_type == 'other':
            self.custom_work_type_input.setText(self.manga_data.get('custom_work_type', ''))
        
        category_names = [cat.get('name') for cat in self.manga_data.get('categories_details', [])]
        self.categories_input.setText(", ".join(category_names))
        
        self.is_nsfw_checkbox.setChecked(self.manga_data.get('is_nsfw', False))
        
        if cover_url := self.manga_data.get('cover_url'):
             self.cover_drop_area.setText(f"Capa Atual:\n{os.path.basename(cover_url)}")
             self.cover_drop_area.setStyleSheet("")

    def _select_cover_image(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Selecionar Imagem de Capa", "", "Imagens (*.png *.jpg *.jpeg *.webp)")
        if file_path:
            self.cover_image_path = file_path
            self.cover_drop_area.setText(f"Nova Capa:\n{os.path.basename(file_path)}")
            self.cover_drop_area.setStyleSheet("")

    @Slot(str)
    def _set_cover_from_drop(self, path_or_url):
        self.cover_image_path = path_or_url
        self.cover_drop_area.setText(f"Nova Capa:\n{os.path.basename(path_or_url)}")
        self.cover_drop_area.setStyleSheet("")

    def get_data(self):
        category_names_from_input = {name.strip().lower() for name in self.categories_input.text().split(',') if name.strip()}
        name_to_id_map = {name.lower(): cat_id for cat_id, name in self.all_categories.items()}
        category_ids = []
        for name in category_names_from_input:
            if name in name_to_id_map:
                category_ids.append(name_to_id_map[name])

        data = {
            "title": self.title_input.text().strip(),
            "alternative_titles": self.alternative_titles_input.text().strip(),
            "description": self.description_input.toPlainText().strip(),
            "author": self.author_input.text().strip(),
            "artist": self.artist_input.text().strip(),
            "status": self.status_combo.currentText(),
            "is_nsfw": self.is_nsfw_checkbox.isChecked(),
            "categories": category_ids,
            "work_type": self.work_type_combo.currentText(),
            "custom_work_type": self.custom_work_type_input.text().strip() if self.work_type_combo.currentText() == 'other' else ''
        }
        
        return data, self.cover_image_path

class MainWindow(QMainWindow):
    task_status_update_requested = Signal(str, str, bool)

    def __init__(self):
        super().__init__()
        self.setWindowOpacity(0.0)
        self.setWindowTitle("Nexus Hybrid Uploader")
        self.setGeometry(100, 100, 1280, 800)
        self.all_categories_cache = {}
        
        self.icon_path = base_path / "assets" / "logo.ico"
        if os.path.exists(self.icon_path):
            self.setWindowIcon(QIcon(str(self.icon_path)))
        
        self.status_server = StatusServer(self)
        self.status_server.message_received.connect(self.handle_bot_message)
        
        self.settings_manager = SettingsManager()
        self.credentials_manager = CredentialsManager()
        self.stats_manager = StatsManager()
        self.api_client = ApiClient()
        self.thread_pool = QThreadPool()
        self.s3_handler = None
        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)
        self.MAX_CONCURRENT_MANGAS = self.settings_manager.get("manga_concurrency", 2)
        self.MAX_CHAPTERS_PER_MANGA = self.settings_manager.get("chapter_concurrency", 1)
        self.manga_task_queues = {}
        self.manga_queue_order = deque()
        self.manga_details_cache = {}
        self.active_chapters_per_manga = {}
        self.active_manga_ids = set()
        self.task_widgets = {}
        self.task_configs = {}
        self.finished_task_ids = set()
        self.running_workers = {}
        self.task_to_manga_map = {}
        self.latest_chapters_online = {}
        self.monitoramento_config = {}
        self.bot_processes = {}
        self.process_to_bot_id_map = {}
        self.is_log_handler_setup = False
        self.bot_start_times = {}
        self.ui_initialized = False
        self.sleep_prevention_active = False
        self.is_quitting = False
        self.task_status_update_requested.connect(self._on_task_status_update)

        self.progress_update_buffer = {}
        self.progress_update_timer = QTimer(self)
        self.progress_update_timer.setSingleShot(True)
        self.progress_update_timer.setInterval(100)
        self.progress_update_timer.timeout.connect(self._process_progress_updates)

        self.login_widget = LoginWidget(credentials_manager=self.credentials_manager)
        self.loading_screen = LoadingScreen()
        
        self.stacked_widget.addWidget(self.login_widget)
        self.stacked_widget.addWidget(self.loading_screen)
        
        self.setup_status_bar()
        self.login_widget.login_requested.connect(self.handle_login_request)
        
        self.countdown_timer = QTimer(self)
        self.scheduled_task_timer = QTimer(self)

    def showEvent(self, event):
        super().showEvent(event)
        if self.windowOpacity() == 0.0:
            self.fade_in_animation = QPropertyAnimation(self, b"windowOpacity")
            self.fade_in_animation.setDuration(300)
            self.fade_in_animation.setStartValue(0.0)
            self.fade_in_animation.setEndValue(1.0)
            self.fade_in_animation.setEasingCurve(QEasingCurve.InOutQuad)
            self.fade_in_animation.start()
        # checa atualização do app uma vez, logo após abrir (fora da UI thread)
        if not getattr(self, "_git_checked", False):
            self._git_checked = True
            QTimer.singleShot(2500, self._maybe_check_git_update)

    def _maybe_check_git_update(self):
        try:
            from core.git_updater import GitUpdater
            from core.workers import GitUpdateWorker
            gu = GitUpdater()
            cfg = gu.load_config()
            if not (cfg.get("auto") and gu.is_configured()):
                return
            worker = GitUpdateWorker(gu)
            worker.signals.finished.connect(self._on_git_update_checked)
            self.thread_pool.start(worker)
        except Exception as e:
            logging.warning(f"Falha ao checar atualização git: {e}")

    @Slot(str, dict)
    def _on_git_update_checked(self, _task, payload):
        status = payload.get("status")
        msg = payload.get("message", "")
        if status == "atualizado":
            try:
                self.status_bar.showMessage(f"Atualização baixada: {msg} Reinicie para aplicar.", 0)
            except Exception:
                pass
            self.show_notification("Atualização baixada",
                                   f"{msg} Reinicie o aplicativo para aplicar.")
        elif status in ("auth", "sem-git", "erro"):
            logging.info(f"[git-update] {status}: {msg}")

    def log_manager_action(self, message):
        full_message = f"[KAORYN] {message}"
        self.append_log_message(full_message)
        logging.info(full_message)

    def create_main_widget(self):
        main_layout = QVBoxLayout()
        self.tab_widget = QTabWidget()
        self.tab_widget.setIconSize(QSize(18, 18))
        from ui.tabs.providers_tab import ProvidersTab

        self.manga_tab = self.create_manga_tab()
        self.monitor_obras_tab = self.create_monitor_obras_tab()
        self.gerenciamento_bots_tab = self.create_gerenciamento_bots_tab()
        # este projeto nao tem editor de provider; a aba esconde o botao sozinha
        self.providers_tab = ProvidersTab(
            ao_importar=self.import_new_provider,
            ao_abrir_pasta=self.open_providers_folder,
            contagem_por_provider=self._contagem_por_provider,
        )
        self.tasks_tab = self.create_tasks_tab()
        self.dashboard_tab = self.create_dashboard_tab()
        self.bot_log_tab = self.create_bot_log_tab()
        self.tab_widget.addTab(self.manga_tab, get_icon("upload-cloud", AppTheme.TEXT_COLOR), " Upload Manual")
        self.tab_widget.addTab(self.monitor_obras_tab, get_icon("monitor", AppTheme.TEXT_COLOR), " Obras Monitoradas")
        self.tab_widget.addTab(self.gerenciamento_bots_tab, get_icon("cpu", AppTheme.TEXT_COLOR), " Gerenciamento de Bots")
        self.tab_widget.addTab(self.providers_tab, get_icon("package", AppTheme.TEXT_COLOR), " Providers")
        self.tab_widget.addTab(self.tasks_tab, get_icon("list", AppTheme.TEXT_COLOR), " Fila de Tarefas")
        self.tab_widget.addTab(self.dashboard_tab, get_icon("bar-chart-2", AppTheme.TEXT_COLOR), " Dashboard")
        self.tab_widget.addTab(self.bot_log_tab, get_icon("file-text", AppTheme.TEXT_COLOR), " Log do Bot")
        main_layout.addWidget(self.tab_widget)
        settings_button = QPushButton(" Configurações")
        settings_button.setIcon(get_icon("settings", AppTheme.TEXT_COLOR))
        settings_button.clicked.connect(self.open_settings_dialog)
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        bottom_layout.addWidget(settings_button)
        main_layout.addLayout(bottom_layout)
        main_widget_container = QWidget()
        main_widget_container.setLayout(main_layout)
        return main_widget_container

    def create_manga_tab(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._create_manga_list_panel(), 1)
        layout.addWidget(self._create_manga_details_panel(), 3)
        return widget

    def _create_manga_list_panel(self):
        panel = QWidget()
        panel.setStyleSheet(f"background-color: {AppTheme.PRIMARY_COLOR};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        title = QLabel("Obras")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Buscar obra...")
        self.search_input.textChanged.connect(self.on_search_text_changed)
        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(500)
        self.search_timer.timeout.connect(lambda: self.refresh_manga_list(self.search_input.text()))
        layout.addWidget(self.search_input)
        self.manga_list_widget = QListWidget()
        self.manga_list_widget.currentItemChanged.connect(self.on_manga_selection_changed)
        layout.addWidget(self.manga_list_widget)
        refresh_button = QPushButton(" Atualizar Lista")
        refresh_button.setIcon(get_icon("refresh-cw", AppTheme.TEXT_COLOR))
        refresh_button.clicked.connect(lambda: self.refresh_manga_list())
        create_manga_button = QPushButton(" Criar Obra")
        create_manga_button.setIcon(get_icon("plus-square", AppTheme.ACCENT_COLOR))
        create_manga_button.clicked.connect(self.handle_create_manga)
        button_layout = QHBoxLayout()
        button_layout.addWidget(refresh_button)
        button_layout.addWidget(create_manga_button)
        layout.addLayout(button_layout)
        return panel

    def _create_manga_details_panel(self):
        panel = QWidget()
        self.manga_details_layout = QVBoxLayout(panel)
        self.manga_details_layout.setContentsMargins(20, 20, 20, 20)
        self.display_manga_details(None)
        return panel

    def create_monitor_obras_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        title = QLabel("Obras Monitoradas")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        config_layout = QHBoxLayout()
        config_layout.addStretch()
        export_button = QPushButton(" Exportar")
        export_button.setIcon(get_icon("share", AppTheme.TEXT_COLOR))
        export_button.clicked.connect(self.export_config)
        import_button = QPushButton(" Importar")
        import_button.setIcon(get_icon("download", AppTheme.TEXT_COLOR))
        import_button.clicked.connect(self.import_config)
        config_layout.addWidget(export_button)
        config_layout.addWidget(import_button)
        layout.addLayout(config_layout)
        search_monitor_layout = QHBoxLayout()
        search_monitor_layout.addWidget(QLabel("Filtrar lista:"))
        self.monitor_search_input = QLineEdit()
        self.monitor_search_input.setPlaceholderText("Buscar por pasta ou obra...")
        self.monitor_search_input.textChanged.connect(self.filter_obras_list)
        search_monitor_layout.addWidget(self.monitor_search_input)
        layout.addLayout(search_monitor_layout)
        self.obras_tree_widget = QTreeWidget()
        self.obras_tree_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.obras_tree_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.obras_tree_widget.customContextMenuRequested.connect(self.show_obras_context_menu)
        self.obras_tree_widget.itemDoubleClicked.connect(self.edit_selected_monitor_folder)
        self.obras_tree_widget.setDragEnabled(True)
        self.obras_tree_widget.setAcceptDrops(False)
        self.obras_tree_widget.setDropIndicatorShown(True)
        self.obras_tree_widget.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        layout.addWidget(self.obras_tree_widget)
        
        button_layout = QHBoxLayout()
        add_button = QPushButton(" Adicionar")
        add_button.setIcon(get_icon("plus", AppTheme.TEXT_COLOR))
        add_button.clicked.connect(self.add_new_monitor_folder)
        edit_button = QPushButton(" Editar")
        edit_button.setIcon(get_icon("edit", AppTheme.TEXT_COLOR))
        edit_button.clicked.connect(self.edit_selected_monitor_folder)
        remove_button = QPushButton(" Remover")
        remove_button.setIcon(get_icon("trash-2", AppTheme.TEXT_COLOR))
        remove_button.clicked.connect(self.remove_selected_monitor_folder)
        download_button = QPushButton(" Baixar Obra(s) Selecionada(s)")
        download_button.setIcon(get_icon("download", AppTheme.TEXT_COLOR))
        download_button.clicked.connect(self.start_download_selected_works)
        button_layout.addWidget(add_button)
        button_layout.addWidget(edit_button)
        button_layout.addWidget(remove_button)
        button_layout.addStretch()
        button_layout.addWidget(download_button)
        layout.addLayout(button_layout)
        
        return widget

    def create_gerenciamento_bots_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        title = QLabel("Painel da Gerente Kaoryn")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        
        controls_layout = QHBoxLayout()
        self.start_all_bots_button = QPushButton(" Iniciar Todos")
        self.start_all_bots_button.setIcon(get_icon("play", AppTheme.SUCCESS_COLOR))
        self.start_all_bots_button.clicked.connect(self.start_all_bots)
        self.stop_all_bots_button = QPushButton(" Parar Todos")
        self.stop_all_bots_button.setIcon(get_icon("square", AppTheme.ERROR_COLOR))
        self.stop_all_bots_button.clicked.connect(self.stop_all_bots)
        
        self.force_check_button = QPushButton(" Forçar Verificação Geral")
        self.force_check_button.setIcon(get_icon("refresh-cw", AppTheme.TEXT_COLOR))
        self.force_check_button.clicked.connect(self.force_full_check)
        
        self.bot_status_label = QLabel("Workers Parados")
        self.bot_status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        controls_layout.addWidget(self.start_all_bots_button)
        controls_layout.addWidget(self.stop_all_bots_button)
        controls_layout.addWidget(self.force_check_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.bot_status_label)
        layout.addLayout(controls_layout)
        
        self.bots_tree_widget = QTreeWidget()
        self.bots_tree_widget.setHeaderLabels(["Worker (Bot)", "Obras", "Status", "Próximo Reinício"])
        header = self.bots_tree_widget.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.bots_tree_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bots_tree_widget.customContextMenuRequested.connect(self.show_bots_management_context_menu)
        self.bots_tree_widget.setAcceptDrops(True)
        self.bots_tree_widget.dropEvent = self.on_bots_drop_event
        layout.addWidget(self.bots_tree_widget)
        
        button_layout = QHBoxLayout()
        add_bot_button = QPushButton(" Adicionar Worker")
        add_bot_button.setIcon(get_icon("plus", AppTheme.TEXT_COLOR))
        add_bot_button.clicked.connect(self.add_new_bot)
        remove_bot_button = QPushButton(" Remover Worker Vazio")
        remove_bot_button.setIcon(get_icon("trash-2", AppTheme.TEXT_COLOR))
        remove_bot_button.clicked.connect(self.remove_selected_empty_bot)
        
        button_layout.addWidget(add_bot_button)
        button_layout.addWidget(remove_bot_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)
        
        providers_title = QLabel("Gerenciamento de Providers")
        providers_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(providers_title)
        
        providers_button_layout = QHBoxLayout()
        import_provider_button = QPushButton(" Importar Novo Provider (.py)")
        import_provider_button.setIcon(get_icon("file-plus", AppTheme.TEXT_COLOR))
        import_provider_button.clicked.connect(self.import_new_provider)
        open_providers_folder_button = QPushButton(" Abrir Pasta de Providers")
        open_providers_folder_button.setIcon(get_icon("folder", AppTheme.TEXT_COLOR))
        open_providers_folder_button.clicked.connect(self.open_providers_folder)
        
        providers_button_layout.addWidget(import_provider_button)
        providers_button_layout.addWidget(open_providers_folder_button)
        providers_button_layout.addStretch()
        layout.addLayout(providers_button_layout)
        
        return widget

    def create_tasks_tab(self):
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        top_layout = QHBoxLayout()
        title = QLabel("Fila de Tarefas")
        title.setObjectName("TitleLabel")
        top_layout.addWidget(title)
        top_layout.addStretch()
        top_layout.addWidget(QLabel("Obras Simultâneas:"))
        self.manga_concurrency_spinbox = QSpinBox()
        self.manga_concurrency_spinbox.setRange(1, 10)
        self.manga_concurrency_spinbox.setValue(self.MAX_CONCURRENT_MANGAS)
        self.manga_concurrency_spinbox.valueChanged.connect(self.on_manga_concurrency_changed)
        top_layout.addWidget(self.manga_concurrency_spinbox)
        top_layout.addWidget(QLabel("Capítulos por Obra:"))
        self.chapter_concurrency_spinbox = QSpinBox()
        self.chapter_concurrency_spinbox.setRange(1, 10)
        self.chapter_concurrency_spinbox.setValue(self.MAX_CHAPTERS_PER_MANGA)
        self.chapter_concurrency_spinbox.valueChanged.connect(self.on_chapter_concurrency_changed)
        top_layout.addWidget(self.chapter_concurrency_spinbox)
        self.retry_all_button = QPushButton(" Reenviar Falhas")
        self.retry_all_button.setIcon(get_icon("refresh-cw", AppTheme.TEXT_COLOR))
        self.retry_all_button.clicked.connect(self.retry_all_failed_tasks)
        top_layout.addWidget(self.retry_all_button)
        self.pause_queue_button = QPushButton(" Pausar Fila")
        self.pause_queue_button.setIcon(get_icon("pause", AppTheme.TEXT_COLOR))
        self.pause_queue_button.clicked.connect(self.toggle_queue_pause)
        top_layout.addWidget(self.pause_queue_button)
        clear_button = QPushButton(" Limpar Concluídos")
        clear_button.setIcon(get_icon("check-square", AppTheme.TEXT_COLOR))
        clear_button.clicked.connect(self.clear_finished_tasks)
        top_layout.addWidget(clear_button)
        main_layout.addLayout(top_layout)
        
        self.tasks_tree = QTreeWidget()
        self.tasks_tree.setHeaderLabels(["Tarefa", "Status"])
        header = self.tasks_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tasks_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tasks_tree.customContextMenuRequested.connect(self.show_task_context_menu)
        self.tasks_tree.setDragEnabled(True)
        self.tasks_tree.setAcceptDrops(True)
        self.tasks_tree.setDropIndicatorShown(True)
        self.tasks_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tasks_tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tasks_tree.dragMoveEvent = self.on_tasks_drag_move
        self.tasks_tree.dropEvent = self.on_tasks_drop_event
        main_layout.addWidget(self.tasks_tree)
        return widget

    def create_bot_log_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Log dos Workers em Tempo Real")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setObjectName("LogBox")
        layout.addWidget(self.log_box)
        return widget

    def create_dashboard_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Dashboard de Atividade")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        cards_layout = QHBoxLayout()
        self.uploads_today_label_widget = self._create_stats_card("Uploads Hoje", "0")
        self.uploads_week_label_widget = self._create_stats_card("Uploads na Semana", "0")
        self.uploads_total_label_widget = self._create_stats_card("Uploads Totais (30d)", "0")
        cards_layout.addWidget(self.uploads_today_label_widget)
        cards_layout.addWidget(self.uploads_week_label_widget)
        cards_layout.addWidget(self.uploads_total_label_widget)
        layout.addLayout(cards_layout)
        chart_title = QLabel("Uploads nos Últimos 7 Dias")
        chart_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(chart_title, alignment=Qt.AlignmentFlag.AlignCenter)
        if pg:
            self.plot_widget = pg.PlotWidget()
            self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
            self.plot_widget.getAxis('bottom').setTextPen(AppTheme.TEXT_COLOR)
            self.plot_widget.getAxis('left').setTextPen(AppTheme.TEXT_COLOR)
            self.bar_chart = None
            layout.addWidget(self.plot_widget)
        else:
            layout.addWidget(QLabel("<i>Instale 'pyqtgraph' para visualizar o gráfico de atividades (pip install pyqtgraph).</i>"))
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        return widget

    def _create_stats_card(self, title, value):
        card = QFrame()
        card.setObjectName("StatsCard")
        card_layout = QVBoxLayout(card)
        title_label = QLabel(title)
        title_label.setFont(QFont("Segoe UI", 10))
        title_label.setStyleSheet(f"color: {AppTheme.MUTED_TEXT_COLOR}; background-color: transparent;")
        value_label = QLabel(value)
        value_label.setObjectName("valueLabel")
        value_label.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        value_label.setStyleSheet("background-color: transparent;")
        card_layout.addWidget(title_label)
        card_layout.addWidget(value_label)
        return card

    def start_timers_and_listeners(self):
        self.status_server.start()
        self.countdown_timer.timeout.connect(self.update_bot_countdowns)
        self.countdown_timer.start(1000)
        self.scheduled_task_timer.timeout.connect(self.schedule_next_tasks)
        self.scheduled_task_timer.start(5000)

    def force_full_check(self):
        reply = QMessageBox.question(
            self,
            "Confirmar Verificação Forçada",
            "Kaoryn enviará um sinal para que todos os workers em execução verifiquem imediatamente todas as suas obras.\n\nDeseja continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                signal_file = base_path / "force_check.signal"
                signal_file.write_text(str(time.time()))
                self.show_notification(
                    "Comando Enviado por Kaoryn",
                    "Um sinal de verificação forçada foi enviado para todos os workers ativos."
                )
                self.log_manager_action("Sinal de verificação forçada enviado para todos os workers.")
            except Exception as e:
                QMessageBox.critical(self, "Erro", f"Não foi possível criar o arquivo de sinal: {e}")

    def force_check_selected_obras(self, manga_ids):
        """Sinal de verificação forçada restrito às obras passadas (formato ts|id1,id2)."""
        try:
            signal_file = base_path / "force_check.signal"
            signal_file.write_text(f"{time.time()}|{','.join(manga_ids)}")
            self.status_bar.showMessage(
                f"Sinal enviado: verificação imediata de {len(manga_ids)} obra(s).", 5000)
            self.log_manager_action(
                f"Sinal de verificação forçada enviado para as obras: {', '.join(manga_ids)}.")
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível criar o arquivo de sinal: {e}")

    def _contagem_por_provider(self) -> dict:
        """Quantas obras monitoradas usam cada provider - alimenta a aba Providers."""
        contagem = {}

        def andar(no):
            if isinstance(no, dict):
                nome = no.get('provider')
                if isinstance(nome, str) and nome:
                    contagem[nome] = contagem.get(nome, 0) + 1
                for valor in no.values():
                    andar(valor)
            elif isinstance(no, list):
                for valor in no:
                    andar(valor)

        andar(getattr(self, 'monitoramento_config', None) or {})
        return contagem

    def _get_providers_path(self) -> Path:
        return base_path / "providers"

    def import_new_provider(self):
        providers_path = self._get_providers_path()
        if not providers_path.exists():
            providers_path.mkdir(parents=True, exist_ok=True)
        file_path, _ = QFileDialog.getOpenFileName(self, "Selecionar Arquivo de Provider", "", "Python Files (*.py)")
        if not file_path:
            return
        source_file = Path(file_path)
        destination_file = providers_path / source_file.name
        if destination_file.exists():
            reply = QMessageBox.question(self, "Substituir Arquivo?", f"O provider '{source_file.name}' já existe. Deseja substituí-lo?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return
        try:
            shutil.copy(source_file, destination_file)
            QMessageBox.information(self, "Sucesso", f"O provider '{source_file.name}' foi importado com sucesso.\n\nPode ser necessário reiniciar os workers ou a aplicação para que a mudança tenha efeito.")
            self.update_all_views()
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível copiar o arquivo do provider: {e}")

    def open_providers_folder(self):
        providers_path = self._get_providers_path()
        if not providers_path.exists():
            providers_path.mkdir(parents=True, exist_ok=True)
            QMessageBox.information(self, "Pasta Criada", f"A pasta de providers foi criada em:\n{providers_path}")
        try:
            if sys.platform == "win32":
                os.startfile(providers_path)
            elif sys.platform == "darwin":
                os.system(f'open "{providers_path}"')
            else:
                os.system(f'xdg-open "{providers_path}"')
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível abrir a pasta de providers: {e}")

    def rename_selected_bot(self):
        selected_items = self.bots_tree_widget.selectedItems()
        if not selected_items:
            return
        item = selected_items[0]
        old_bot_id = item.data(0, Qt.ItemDataRole.UserRole)
        new_bot_name, ok = QInputDialog.getText(self, "Renomear Worker", f"Digite o novo nome para '{old_bot_id}':", QLineEdit.EchoMode.Normal, old_bot_id)
        if ok and new_bot_name and new_bot_name != old_bot_id:
            if new_bot_name in self.monitoramento_config:
                QMessageBox.warning(self, "Nome Inválido", "Um worker com este nome já existe.")
                return
            self.monitoramento_config[new_bot_name] = self.monitoramento_config.pop(old_bot_id)
            self._save_monitoring_config()
            self.update_all_views()
            self.show_notification("Worker Renomeado", f"O worker '{old_bot_id}' foi renomeado para '{new_bot_name}'.")

    @Slot(dict)
    def handle_bot_message(self, message):
        if not self.ui_initialized:
            logging.warning("GUI não inicializada. Mensagem do bot ignorada.")
            return

        event = message.get("event")
        data = message.get("data", {})
        if not event:
            return

        bot_id = data.get("bot_id", "bot_?")
        
        if event == "enqueue_upload_task":
            manga_id = data.get("manga_id")
            chapter_config = data.get("chapter_config")

            if not manga_id or not chapter_config:
                logging.error(f"Mensagem 'enqueue_upload_task' malformada recebida do bot {bot_id}: {data}")
                return

            manga_title = self.manga_details_cache.get(str(manga_id), {}).get('title', f"Obra ID: {manga_id}")

            final_config = {
                'task_id': chapter_config.get('task_id', f"bot-{bot_id}-{time.time()}"),
                'path': chapter_config.get('path'),
                'images': chapter_config.get('images', []),
                'number': chapter_config.get('number'),
                'display_name': chapter_config.get('display_name'),
                'manga_id': manga_id,
                'manga_title': manga_title,
                'temp_dir_to_clean': chapter_config.get('temp_dir_to_clean'),
                'access_level': 'public',
                'vip_duration_hours': None,
                'coin_cost': None,
                'scheduled_time': None
            }
            
            self.log_manager_action(f"Worker '{bot_id}' solicitou upload para '{manga_title}', Cap. {final_config['number']}.")
            self.start_processing_and_upload([final_config], manga_id, manga_title, bot_id=bot_id)

    @Slot(str)
    def append_log_message(self, message):
        if hasattr(self, 'log_box'):
            self.log_box.appendPlainText(message.strip())
            self.log_box.moveCursor(QTextCursor.MoveOperation.End)

    @Slot()
    def handle_bot_output(self):
        process = self.sender()
        bot_id = self.process_to_bot_id_map.get(process, "WORKER_DESCONHECIDO")
        data = process.readAllStandardOutput().data().decode(errors='ignore')
        for line in data.strip().split('\n'):
            self.append_log_message(f"[{bot_id.upper()}]: {line.strip()}")
    
    @Slot()
    def on_bot_process_finished(self):
        process = self.sender()
        if process in self.process_to_bot_id_map:
            bot_id = self.process_to_bot_id_map[process]
            if self.ui_initialized:
                for i in range(self.bots_tree_widget.topLevelItemCount()):
                    item = self.bots_tree_widget.topLevelItem(i)
                    if item.data(0, Qt.ItemDataRole.UserRole) == bot_id:
                        item.setText(2, "FALHOU")
                        item.setForeground(2, QColor(AppTheme.ERROR_COLOR))
                        item.setText(3, "---")
                        break
            if bot_id in self.bot_processes: del self.bot_processes[bot_id]
            del self.process_to_bot_id_map[process]
            self.bot_start_times.pop(bot_id, None)
            if not self.bot_processes:
                self.allow_sleep()
            self.log_manager_action(f"PROCESSO DO WORKER [{bot_id.upper()}] FOI FINALIZADO INESPERADAMENTE.")
            self.show_notification("Kaoryn: Worker Falhou", f"O worker '{bot_id}' parou inesperadamente.", QSystemTrayIcon.MessageIcon.Critical)
            self.update_bot_status_label()
    
    @Slot(str, dict)
    def _on_task_finished(self, task_id, payload):
        status_message = payload.get('message', 'Concluído')
        
        self.task_status_update_requested.emit(task_id, status_message, False)
        
        if not task_id.startswith("download-"):
            self.stats_manager.record_upload_success()
            if task_id in self.task_configs:
                config = self.task_configs[task_id]
                title = config.get('manga_title', 'Upload')
                number = config.get('number', '')
                self.show_notification(f"Upload Concluído: {title}", f"O capítulo {number} foi enviado com sucesso.")
        else:
            if task_id in self.task_configs:
                config = self.task_configs[task_id]
                title = config.get('manga_title', 'Obra')
                self.show_notification(f"Download Concluído", f"O download de '{title}' foi concluído.")

        if self.ui_initialized and self.tab_widget.currentWidget() == self.dashboard_tab:
            self.update_dashboard()

    @Slot(str, dict)
    def _on_task_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido')

        self.task_status_update_requested.emit(task_id, str(error_message), True)

        if task_id in self.task_configs:
            config = self.task_configs[task_id]
            title = config.get('manga_title', 'Tarefa')
            if task_id.startswith("download-"):
                self.show_notification(f"Falha no Download: {title}", f"{error_message}", QSystemTrayIcon.MessageIcon.Critical)
            else:
                number = config.get('number', '')
                self.show_notification(f"Falha no Upload: {title}", f"Capítulo {number}: {error_message}", QSystemTrayIcon.MessageIcon.Critical)

    @Slot(str, str, bool)
    def _on_task_status_update(self, task_id, message, is_error=False):
        manga_id = self.task_to_manga_map.get(task_id)
        if not (manga_id in self.task_widgets and task_id in self.task_widgets[manga_id]['chapters']):
            return
            
        chapter_data = self.task_widgets[manga_id]['chapters'][task_id]
        
        if chapter_data.get('final_status'):
            return
            
        item = chapter_data.get('item')
        if not item: return

        final_status = 'error' if is_error else 'success'
        color = QColor(AppTheme.ERROR_COLOR) if is_error else QColor(AppTheme.SUCCESS_COLOR)

        if "Cancelado" in message or "Removido" in message:
            final_status = 'cancelled'
            color = QColor(AppTheme.MUTED_TEXT_COLOR)
        
        chapter_data['final_status'] = final_status
        
        if self.tasks_tree.itemWidget(item, 1):
            self.tasks_tree.setItemWidget(item, 1, None)
        
        if 'progress_bar' in chapter_data:
            del chapter_data['progress_bar']
            
        item.setText(1, message)
        item.setForeground(1, color)

        self._finalize_task(task_id)

    def closeEvent(self, event):
        if self.settings_manager.get("minimize_to_tray") and not self.is_quitting and hasattr(self, 'tray_icon'):
            event.ignore()
            self.hide()
            self.show_notification("Aplicativo Minimizado", "O Hybrid Uploader continua rodando em segundo plano.")
        else:
            self.quit_application()

    def quit_application(self):
        self.is_quitting = True
        self.allow_sleep()
        self.stop_all_bots()
        self.status_server.stop()
        self.thread_pool.clear()
        self.thread_pool.waitForDone(-1)
        QApplication.quit()

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(QIcon(str(self.icon_path)), self)
        self.tray_icon.setToolTip("Nexus Hybrid Uploader")
        menu = QMenu()
        show_action = QAction("Mostrar", self)
        show_action.triggered.connect(self.showNormal)
        start_all_action = QAction("Iniciar Todos os Workers", self)
        start_all_action.triggered.connect(self.start_all_bots)
        stop_all_action = QAction("Parar Todos os Workers", self)
        stop_all_action.triggered.connect(self.stop_all_bots)
        quit_action = QAction("Sair", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(start_all_action)
        menu.addAction(stop_all_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isMinimized() or not self.isVisible():
                self.showNormal()
            self.activateWindow()
            
    def show_notification(self, title, message, icon=QSystemTrayIcon.MessageIcon.Information):
        if hasattr(self, 'tray_icon') and QSystemTrayIcon.isSystemTrayAvailable() and self.settings_manager.get("enable_notifications"):
            self.tray_icon.showMessage(title, message, icon, 5000)

    def prevent_sleep(self):
        if os.name == 'nt' and not self.sleep_prevention_active:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            self.sleep_prevention_active = True
            self.log_manager_action("Modo de suspensão do Windows desativado para manter os workers ativos.")
            logging.info("Windows sleep mode prevented.")

    def allow_sleep(self):
        if os.name == 'nt' and self.sleep_prevention_active:
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.sleep_prevention_active = False
            self.log_manager_action("Modo de suspensão do Windows reativado.")
            logging.info("Windows sleep mode allowed.")

    def open_settings_dialog(self):
        dialog = SettingsDialog(self.settings_manager, self.credentials_manager, self)
        if dialog.exec():
            self.login_widget.load_credentials()
            self.MAX_CONCURRENT_MANGAS = self.settings_manager.get("manga_concurrency", 2)
            self.MAX_CHAPTERS_PER_MANGA = self.settings_manager.get("chapter_concurrency", 1)
            self.manga_concurrency_spinbox.setValue(self.MAX_CONCURRENT_MANGAS)
            self.chapter_concurrency_spinbox.setValue(self.MAX_CHAPTERS_PER_MANGA)

    def handle_login_request(self, username, password):
        self.login_widget.show_loading()
        s3_path = self.settings_manager.get("s3_config_path")

        worker = LoginWorker(self.api_client, username, password, s3_path)
        worker.signals.finished.connect(self._on_login_finished)
        worker.signals.error.connect(self._on_login_error)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_login_finished(self, task_id, payload):
        self.credentials_manager.save_credentials(self.api_client.username, self.api_client.password)
        s3_config_path = self.settings_manager.get("s3_config_path")
        self.s3_handler = ContaboS3Handler(config_path=s3_config_path)
        self.check_and_migrate_config()
        self._load_monitoring_config()
        self.initialize_main_ui()
        self.start_background_cache_loading()
        self.load_categories_cache()

    @Slot(str, dict)
    def _on_login_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido durante o login.')
        self.login_widget.show_form()
        QMessageBox.critical(self, "Erro de Login", error_message)

    def initialize_main_ui(self):
        if self.ui_initialized:
            return

        if not self.is_log_handler_setup:
            log_handler = QtLogHandler()
            log_handler.new_log_record.connect(self.append_log_message)
            logging.getLogger().addHandler(log_handler)
            self.is_log_handler_setup = True
        
        self.main_widget = self.create_main_widget()
        self.stacked_widget.addWidget(self.main_widget)
        self.ui_initialized = True
        self.setup_tray_icon()
        self.start_timers_and_listeners()
        self.update_all_views() 
        self.refresh_manga_list()
        
        self.status_label.setText(f"<b><font color='{AppTheme.SUCCESS_COLOR}'>Conectado: {self.api_client.username}</font></b>")
        self.reconnect_button.setVisible(True)
        self.stacked_widget.setCurrentWidget(self.main_widget)

    def start_background_cache_loading(self):
        all_manga_ids = set()
        for bot_config in self.monitoramento_config.values():
            monitors = {k: v for k, v in bot_config.items() if not k.startswith('_')}
            for manga_config in monitors.values():
                manga_id = manga_config.get("manga_id")
                if manga_id:
                    all_manga_ids.add(manga_id)
        
        if not all_manga_ids:
            self.log_manager_action("Nenhuma obra monitorada para carregar detalhes.")
            return

        self.log_manager_action(f"Iniciando carregamento em segundo plano dos detalhes de {len(all_manga_ids)} obra(s)...")
        worker = CacheMangaDetailsWorker(self.api_client, list(all_manga_ids))
        worker.signals.finished.connect(self._on_background_cache_event)
        worker.signals.error.connect(self._on_cache_worker_error)
        worker.signals.status_update.connect(
            lambda _, msg: self.status_bar.showMessage(msg.split(';')[-1], 2000)
        )
        self.thread_pool.start(worker)
    
    @Slot(str, dict)
    def _on_background_cache_event(self, event_type, payload):
        if event_type == "single_manga_detail":
            manga_id = payload.get('manga_id')
            data = payload.get('data')
            if manga_id and data:
                self.manga_details_cache[manga_id] = data
                self._update_ui_with_manga_title(manga_id, data.get('title', f"ID: {manga_id}"))

        elif event_type == "cache_complete":
            final_cache = payload.get('cache', {})
            self.manga_details_cache.update(final_cache)
            self.log_manager_action("Carregamento de detalhes em segundo plano concluído.")
            self.status_bar.showMessage("Detalhes das obras carregados.", 3000)
            self.populate_obras_list()

    def _update_ui_with_manga_title(self, manga_id, new_title):
        if not self.ui_initialized: return
        
        for i in range(self.obras_tree_widget.topLevelItemCount()):
            item = self.obras_tree_widget.topLevelItem(i)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            item_manga_id = data.get('config', {}).get('manga_id')
            
            if str(item_manga_id) == str(manga_id):
                item.setText(0, new_title)
                break

    def load_initial_data(self):
        pass

    def cache_monitored_manga_titles(self):
        pass
        
    def on_cache_loaded(self):
        pass

    @Slot(str, dict)
    def _on_cache_worker_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido ao carregar cache.')
        self.log_manager_action(f"ERRO: Falha ao carregar detalhes das obras: {error_message}")
        QMessageBox.warning(self, "Erro ao Carregar Detalhes", f"Não foi possível buscar os detalhes de algumas obras em segundo plano:\n{error_message}")
        
    def load_categories_cache(self):
        self.log_manager_action("Buscando lista de categorias em segundo plano...")
        worker = CategoryFetcher(self.api_client)
        worker.signals.finished.connect(self._on_categories_loaded)
        worker.signals.error.connect(self._on_categories_load_error)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_categories_loaded(self, task_id, payload):
        categories = payload.get('categories', [])
        self.all_categories_cache = {cat['id']: cat['name'] for cat in categories}
        self.log_manager_action(f"{len(self.all_categories_cache)} categorias carregadas com sucesso.")

    @Slot(str, dict)
    def _on_categories_load_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido.')
        self.log_manager_action(f"ERRO: Falha ao buscar lista de categorias. Detalhes: {error_message}")
        QMessageBox.critical(self, "Erro de Rede", f"Não foi possível carregar a lista de categorias do site.\n\nDetalhes: {error_message}")

    @Slot(str, str)
    def _on_worker_status_update(self, task_id, message):
        if hasattr(self, 'status_bar'):
            self.status_bar.showMessage(message, 0)

    def handle_create_manga(self):
        if not self.all_categories_cache:
            QMessageBox.warning(self, "Aguarde", "A lista de categorias ainda não foi carregada.")
            return
        dialog = MangaEditDialog(self, self.all_categories_cache)
        if dialog.exec():
            manga_data, cover_path = dialog.get_data()
            self.setEnabled(False)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.status_bar.showMessage("Iniciando criação da obra...", 0)
            
            if not cover_path:
                worker = MangaUpdateWorker(self.api_client, manga_data)
                worker.signals.status_update.connect(self._on_worker_status_update)
                worker.signals.finished.connect(self._on_manga_update_finished)
                worker.signals.error.connect(self._on_manga_update_error)
                self.thread_pool.start(worker)
            else:
                cover_worker = CoverUploadWorker(self.s3_handler, cover_path, manga_data.get('title', ''), manga_id=None)
                cover_worker.signals.status_update.connect(self._on_worker_status_update)
                cover_worker.signals.finished.connect(
                    lambda task_id, payload: self._on_cover_upload_for_manga_update(payload, manga_data, None)
                )
                cover_worker.signals.error.connect(self._on_manga_update_error)
                self.thread_pool.start(cover_worker)

    def handle_edit_manga(self, manga_data):
        if not self.all_categories_cache:
            QMessageBox.warning(self, "Aguarde", "A lista de categorias ainda não foi carregada.")
            return
            
        manga_id = manga_data.get('id')
        dialog = MangaEditDialog(self, self.all_categories_cache, manga_data=manga_data)
        
        if dialog.exec():
            updated_data, cover_path = dialog.get_data()
            self.setEnabled(False)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.status_bar.showMessage("Iniciando atualização da obra...", 0)

            if not cover_path:
                worker = MangaUpdateWorker(self.api_client, updated_data, manga_id=manga_id)
                worker.signals.status_update.connect(self._on_worker_status_update)
                worker.signals.finished.connect(self._on_manga_update_finished)
                worker.signals.error.connect(self._on_manga_update_error)
                self.thread_pool.start(worker)
            else:
                cover_worker = CoverUploadWorker(self.s3_handler, cover_path, updated_data.get('title', ''), manga_id=manga_id)
                cover_worker.signals.status_update.connect(self._on_worker_status_update)
                cover_worker.signals.finished.connect(
                    lambda task_id, payload: self._on_cover_upload_for_manga_update(payload, updated_data, manga_id)
                )
                cover_worker.signals.error.connect(self._on_manga_update_error)
                self.thread_pool.start(cover_worker)

    @Slot(dict, dict, object)
    def _on_cover_upload_for_manga_update(self, cover_payload, manga_data_to_send, manga_id_to_update):
        try:
            object_key = cover_payload.get('object_key')
            if not object_key:
                raise ValueError('Worker de upload de capa não retornou um object_key válido.')
            
            manga_data_to_send['cover_image_url'] = object_key 

            def start_next_worker():
                worker = MangaUpdateWorker(self.api_client, manga_data_to_send, manga_id=manga_id_to_update)
                worker.signals.finished.connect(self._on_manga_update_finished)
                worker.signals.error.connect(self._on_manga_update_error)
                self.thread_pool.start(worker)
            
            QTimer.singleShot(0, start_next_worker)

        except Exception as e:
            self._on_manga_update_error("cover_bridge", {'message': str(e)})

    def start_manga_update_worker(self, manga_data, manga_id):
        worker = MangaUpdateWorker(self.api_client, manga_data, manga_id=manga_id)
        worker.signals.status_update.connect(self._on_worker_status_update)
        worker.signals.finished.connect(self._on_manga_update_finished)
        worker.signals.error.connect(self._on_manga_update_error)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_manga_update_finished(self, task_id, response_data):
        QApplication.restoreOverrideCursor()
        self.setEnabled(True)
        
        def finish_actions():
            title = response_data.get('title', '')
            manga_id = response_data.get('id')
            action_str = "atualizada" if manga_id and self.manga_details_cache.get(str(manga_id)) else "criada"
            QMessageBox.information(self, "Sucesso", f"A obra '{title}' foi {action_str} com sucesso!")
            self.refresh_manga_list(q=title)
        
        QTimer.singleShot(0, finish_actions)


    @Slot(str, dict)
    def _on_manga_update_error(self, task_id, payload):
        QApplication.restoreOverrideCursor()
        self.setEnabled(True)

        def error_actions():
            error_message = payload.get('message', 'Erro desconhecido.')
            QMessageBox.critical(self, "Erro ao Salvar Obra", str(error_message))

        QTimer.singleShot(0, error_actions)

    def start_processing_and_upload(self, chapters_to_upload, manga_id, manga_title=None, bot_id=None):
        if not chapters_to_upload: return
        if not manga_title:
            manga_title = self.manga_details_cache.get(str(manga_id), {}).get('title', f"Obra ID: {manga_id}")
        
        online_chapters = set(self.latest_chapters_online.get(str(manga_id), []))
        chapters_na_fila = []
        chapters_ignorados = []

        for config in chapters_to_upload:
            if config['number'] in online_chapters and not config.get('overwrite'):
                chapters_ignorados.append(config['number'])
            else:
                chapters_na_fila.append(config)
        
        if chapters_ignorados:
            msg = f"Os seguintes capítulos para <i>'{manga_title}'</i> já existem no site e foram ignorados:\n<b>{', '.join(chapters_ignorados)}</b>"
            if bot_id:
                self.log_manager_action(f"Worker '{bot_id}' tentou enviar capítulos duplicados para '{manga_title}': {', '.join(chapters_ignorados)}")
            else:
                QMessageBox.information(self, "Capítulos Ignorados", msg)

        if not chapters_na_fila: return
        
        for chapter_config in chapters_na_fila:

            if manga_id not in self.manga_task_queues:
                self.manga_task_queues[manga_id] = deque()
            if manga_id not in self.manga_queue_order:
                self.manga_queue_order.append(manga_id)
                
            self.manga_task_queues[manga_id].append(chapter_config)
            self.add_task_to_manager(manga_id, manga_title, chapter_config, bot_id=bot_id)
            
        self.schedule_next_tasks()

    def toggle_queue_pause(self):
        self.queue_paused = not getattr(self, 'queue_paused', False)
        if self.queue_paused:
            self.pause_queue_button.setText(" Retomar Fila")
            self.pause_queue_button.setIcon(get_icon("play", AppTheme.SUCCESS_COLOR))
            self.status_bar.showMessage("Fila pausada: tarefas em andamento terminam, as demais aguardam.", 5000)
        else:
            self.pause_queue_button.setText(" Pausar Fila")
            self.pause_queue_button.setIcon(get_icon("pause", AppTheme.TEXT_COLOR))
            self.status_bar.showMessage("Fila retomada.", 3000)
            self.schedule_next_tasks()

    def retry_all_failed_tasks(self):
        falhas = []
        for manga_id, manga_dict in self.task_widgets.items():
            for task_id, data in manga_dict['chapters'].items():
                if data.get('final_status') == 'error':
                    falhas.append(task_id)
        if not falhas:
            self.status_bar.showMessage("Nenhuma tarefa com falha para reenviar.", 3000)
            return
        for task_id in falhas:
            self.retry_failed_task(task_id)
        self.status_bar.showMessage(f"{len(falhas)} tarefa(s) com falha reenviada(s) para a fila.", 4000)

    def schedule_next_tasks(self):
        if getattr(self, 'queue_paused', False):
            return
        active_manga_count = len(self.active_manga_ids)
        
        for manga_id in list(self.manga_queue_order):
            if manga_id not in self.manga_task_queues or not self.manga_task_queues[manga_id]:
                continue
            
            can_start_new_manga = active_manga_count < self.MAX_CONCURRENT_MANGAS
            is_manga_active = manga_id in self.active_manga_ids
            chapters_in_progress = self.active_chapters_per_manga.get(manga_id, 0)
            
            if chapters_in_progress < self.MAX_CHAPTERS_PER_MANGA:
                if is_manga_active or can_start_new_manga:
                    task_to_start = self.manga_task_queues[manga_id].popleft()
                    self.active_manga_ids.add(manga_id)
                    self.active_chapters_per_manga[manga_id] = chapters_in_progress + 1
                    if not is_manga_active:
                        active_manga_count += 1
                    self.start_chapter_worker(task_to_start)

    def start_chapter_worker(self, chapter_config):
        task_id = chapter_config['task_id']
        
        temp_dir_to_clean = chapter_config.get('temp_dir_to_clean')
        worker = ChapterUploadTask(task_id, self.api_client, self.s3_handler, chapter_config, temp_dir_to_clean=temp_dir_to_clean)

        worker.signals.finished.connect(self._on_task_finished)
        worker.signals.error.connect(self._on_task_error)
        worker.signals.progress.connect(self.update_task_progress)
        worker.signals.status_update.connect(self.update_task_status)
        self.running_workers[task_id] = worker
        self.thread_pool.start(worker)
        
    def start_download_selected_works(self):
        selected_items = self.obras_tree_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "Nenhuma Obra Selecionada", "Por favor, selecione uma ou mais obras na lista para baixar.")
            return
        dest_path = QFileDialog.getExistingDirectory(self, "Selecione a Pasta de Destino")
        if not dest_path: return
        for item in selected_items:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            manga_config = data.get('config', {})
            manga_id = manga_config.get('manga_id')
            if not manga_id:
                logging.warning(f"Item selecionado sem manga_id: {item.text(0)}")
                continue
            manga_title = self.manga_details_cache.get(str(manga_id), {}).get('title', item.text(0))
            task_id = f"download-{manga_id}-{time.time()}"
            chapter_config_mock = {'task_id': task_id, 'path': manga_title, 'manga_title': manga_title}
            self.add_task_to_manager(manga_id, manga_title, chapter_config_mock, is_download=True)
            worker = MangaDownloadWorker(task_id, self.api_client, manga_id, manga_title, dest_path)
            worker.signals.finished.connect(self._on_task_finished)
            worker.signals.error.connect(self._on_task_error)
            worker.signals.progress.connect(self.update_task_progress)
            worker.signals.status_update.connect(self.update_task_status)
            self.running_workers[task_id] = worker
            self.thread_pool.start(worker)

    def _finalize_task(self, task_id):
        if task_id in self.finished_task_ids: return
        self.finished_task_ids.add(task_id)
        
        manga_id = self.task_to_manga_map.get(task_id)
        if manga_id and manga_id in self.active_chapters_per_manga:
            self.active_chapters_per_manga[manga_id] -= 1
            if self.active_chapters_per_manga[manga_id] == 0:
                self.active_manga_ids.discard(manga_id)
                del self.active_chapters_per_manga[manga_id]
        
        if manga_id in self.manga_task_queues and not self.manga_task_queues[manga_id]:
            self.manga_queue_order = deque([mid for mid in self.manga_queue_order if mid != manga_id])
            
        if task_id in self.running_workers:
            del self.running_workers[task_id]
            
        QTimer.singleShot(10, self.schedule_next_tasks)

    def _get_config_path(self):
        return base_path / "monitoramento_bots.json"

    def _load_monitoring_config(self):
        config_path = self._get_config_path()
        if not config_path.exists():
            self.monitoramento_config = {}
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.monitoramento_config = json.load(f)
        except Exception as e:
            logging.error(f"Erro ao carregar '{config_path.name}': {e}")
            self.monitoramento_config = {}

    def _save_monitoring_config(self):
        config_path = self._get_config_path()
        if config_path.exists():
            try:
                backup_path = config_path.with_suffix(".json.bak")
                shutil.copy(config_path, backup_path)
            except Exception as e:
                logging.warning(f"Não foi possível criar o backup da configuração: {e}")
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.monitoramento_config, f, indent=4)
            logging.info(f"Configuração '{config_path.name}' salva.")
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar a configuração: {e}")

    def check_and_migrate_config(self):
        old_config_path = base_path / "monitoramento.json"
        new_config_path = self._get_config_path()
        if old_config_path.exists() and not new_config_path.exists():
            self.log_manager_action("Arquivo de configuração antigo detectado. Iniciando migração...")
            try:
                with open(old_config_path, 'r', encoding='utf-8') as f:
                    old_config = json.load(f)
                if not old_config:
                    old_config_path.rename(f"{old_config_path}.migrated_empty")
                    return
                num_obras = len(old_config)
                num_bots, ok = QInputDialog.getInt(self, "Migração de Configuração", f"Detectamos {num_obras} obra(s) em sua configuração antiga.\nEm quantos workers você deseja distribuí-las?", 1, 1, 10, 1)
                if ok:
                    new_config = {f"bot_{i+1}": {} for i in range(num_bots)}
                    bot_ids = list(new_config.keys())
                    for i, (folder, manga_config) in enumerate(old_config.items()):
                        bot_to_assign = bot_ids[i % num_bots]
                        new_config[bot_to_assign][folder] = manga_config
                    self.monitoramento_config = new_config
                    self._save_monitoring_config()
                    old_config_path.rename(f"{old_config_path}.migrated")
                    QMessageBox.information(self, "Migração Concluída", f"Suas {num_obras} obra(s) foram distribuídas em {num_bots} worker(s).\nO arquivo antigo foi renomeado para 'monitoramento.json.migrated'.")
                else:
                    QMessageBox.warning(self, "Migração Cancelada", "A migração foi cancelada. O programa usará um arquivo de configuração vazio.")
            except Exception as e:
                QMessageBox.critical(self, "Erro na Migração", f"Ocorreu um erro ao migrar a configuração: {e}")

    def setup_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Aguardando login...")
        self.status_bar.addWidget(self.status_label)
        self.reconnect_button = QPushButton(" Reconectar")
        self.reconnect_button.setIcon(get_icon("zap", AppTheme.TEXT_COLOR))
        self.reconnect_button.clicked.connect(self.handle_manual_reconnect)
        self.reconnect_button.setVisible(False)
        self.status_bar.addPermanentWidget(self.reconnect_button)

    def handle_manual_reconnect(self):
        if not self.api_client.username: return
        self.status_label.setText("Reconectando...")
        self.reconnect_button.setEnabled(False)

        worker = LoginWorker(self.api_client, self.api_client.username, self.api_client.password, self.settings_manager.get("s3_config_path"))
        worker.signals.finished.connect(self._on_reconnect_success)
        worker.signals.error.connect(self._on_reconnect_error)
        self.thread_pool.start(worker)
        
    @Slot(str,dict)
    def _on_reconnect_success(self, task_id,payload):
        self.status_label.setText(f"<b><font color='{AppTheme.SUCCESS_COLOR}'>Conectado: {self.api_client.username}</font></b>")
        self.reconnect_button.setEnabled(True)
        self.refresh_manga_list()

    @Slot(str,dict)
    def _on_reconnect_error(self, task_id,payload):
        self.status_label.setText(f"<b><font color='{AppTheme.ERROR_COLOR}'>Falha na reconexão</font></b>")
        self.reconnect_button.setEnabled(True)

    def start_all_bots(self):
        self.log_manager_action("Enviando comando para iniciar todos os workers.")
        for bot_id in self.monitoramento_config: self.start_bot(bot_id)

    def stop_all_bots(self):
        self.log_manager_action("Enviando comando para parar todos os workers.")
        for bot_id in list(self.bot_processes.keys()): self.stop_bot(bot_id)

    def start_bot(self, bot_id):
        if bot_id in self.bot_processes and self.bot_processes[bot_id].state() == QProcess.ProcessState.Running:
            return

        if not self.bot_processes:
            self.prevent_sleep()

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.handle_bot_output)
        process.finished.connect(self.on_bot_process_finished)

        self.bot_processes[bot_id] = process
        self.process_to_bot_id_map[process] = bot_id
        self.bot_start_times[bot_id] = time.time()
        self.log_manager_action(f"Iniciando processo do worker '{bot_id}'.")

        bot_executable_name = "run_bot_console.exe" if os.name == 'nt' else "run_bot_console"
        bot_executable_path = base_path / bot_executable_name
        args = ['--bot-id', bot_id]

        if bot_executable_path.exists():
            process.start(str(bot_executable_path), args)
        else:
            if os.name == 'nt':
                venv_python_path = base_path / "venv" / "Scripts" / "python.exe"
            else:
                venv_python_path = base_path / "venv" / "bin" / "python"

            bot_script_path = base_path / "run_bot.py"
            
            if venv_python_path.exists() and bot_script_path.exists():
                self.log_manager_action(f"Iniciando worker '{bot_id}' com interpretador do venv: {venv_python_path}")
                process.start(str(venv_python_path), [str(bot_script_path)] + args)
            elif bot_script_path.exists():
                self.log_manager_action(f"AVISO: venv não encontrado. Usando o interpretador do sistema para o worker '{bot_id}': {sys.executable}")
                process.start(sys.executable, [str(bot_script_path)] + args)
            else:
                error_msg = f"ERRO CRÍTICO: Não foi possível encontrar 'run_bot.py' para iniciar o worker '{bot_id}'."
                self.log_manager_action(error_msg)
                QMessageBox.critical(self, "Erro ao Iniciar Worker", error_msg)
                return

        self.update_bot_views()
        self.show_notification("Kaoryn: Worker Ativado", f"O worker '{bot_id}' foi iniciado.")

    def stop_bot(self, bot_id):
        if bot_id in self.bot_processes and self.bot_processes[bot_id].state() == QProcess.ProcessState.Running:
            process = self.bot_processes[bot_id]
            process.finished.disconnect(self.on_bot_process_finished)
            process.kill()
            process.waitForFinished()
            self.log_manager_action(f"Processo do worker '{bot_id}' parado via comando.")
        if bot_id in self.bot_processes:
            process = self.bot_processes.pop(bot_id)
            if process in self.process_to_bot_id_map:
                del self.process_to_bot_id_map[process]
        self.bot_start_times.pop(bot_id, None)
        if not self.bot_processes: self.allow_sleep()
        self.update_bot_views()
        self.show_notification("Kaoryn: Worker Desativado", f"O worker '{bot_id}' foi parado.")

    def update_bot_views(self):
        self.populate_bots_list()
        self.update_bot_status_label()

    def update_all_views(self):
        self.populate_obras_list()
        self.populate_bots_list()
        self.update_bot_status_label()

    def update_bot_status_label(self):
        running_bots = len(self.bot_processes)
        total_bots = len(self.monitoramento_config)
        if hasattr(self, 'bot_status_label'):
            self.bot_status_label.setText(f"<b>Status:</b> {running_bots}/{total_bots} workers rodando")

    @staticmethod
    def _formata_data_curta(iso_str):
        if not iso_str:
            return "—"
        try:
            return datetime.fromisoformat(iso_str).strftime("%d/%m %H:%M")
        except (ValueError, TypeError):
            return "—"

    def _status_fresco_do_disco(self):
        """
        O bot grava last_check_at/next_check_at/last_new_chapter direto no
        monitoramento_bots.json; a cópia em memória da GUI não vê isso. Aqui os
        campos de STATUS são lidos frescos do disco, sem mexer na cópia em
        memória (que pode ter edições não salvas).
        """
        try:
            with open(self._get_config_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def populate_obras_list(self):
        self.obras_tree_widget.clear()
        self.obras_tree_widget.setHeaderLabels(
            ["Obra", "Pasta / URL", "Tipo", "Worker", "Último Novo Cap", "Última Verif.", "Próxima Verif."])

        scrollbar = self.obras_tree_widget.verticalScrollBar()
        old_scroll_pos = scrollbar.value()
        status_disco = self._status_fresco_do_disco()

        for bot_id, bot_config in sorted(self.monitoramento_config.items()):
            obra_configs = {k: v for k, v in bot_config.items() if not k.startswith('_')}
            for config_key, manga_config in sorted(obra_configs.items()):
                manga_id = manga_config.get("manga_id")

                manga_title = self.manga_details_cache.get(str(manga_id), {}).get('title', f"ID: {manga_id} (Carregando...)")

                status = status_disco.get(bot_id, {}).get(config_key, manga_config)
                ultimo_cap = status.get("last_new_chapter") or "—"
                ultima_verif = self._formata_data_curta(status.get("last_check_at"))
                proxima_verif = self._formata_data_curta(status.get("next_check_at"))

                bot_display_name = f"Worker {bot_id.replace('bot_', '')}"
                if 'provider' in manga_config:
                    display_path = manga_config.get('manga_url', 'URL não definida')
                    monitor_type = "Provider"
                    item = QTreeWidgetItem([manga_title, display_path, monitor_type, bot_display_name,
                                            ultimo_cap, ultima_verif, proxima_verif])
                    item.setIcon(2, get_icon("cloud", AppTheme.TEXT_COLOR))
                else:
                    display_path = config_key
                    monitor_type = "Pasta Local"
                    item = QTreeWidgetItem([manga_title, display_path, monitor_type, bot_display_name,
                                            "—", "—", "—"])
                    item.setIcon(2, get_icon("folder", AppTheme.TEXT_COLOR))

                item.setData(0, Qt.ItemDataRole.UserRole, {'bot_id': bot_id, 'config_key': config_key, 'config': manga_config})
                self.obras_tree_widget.addTopLevelItem(item)

        header = self.obras_tree_widget.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4, 5, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.filter_obras_list()
        scrollbar.setValue(old_scroll_pos)

    def filter_obras_list(self):
        filter_text = self.monitor_search_input.text().lower()
        for i in range(self.obras_tree_widget.topLevelItemCount()):
            item = self.obras_tree_widget.topLevelItem(i)
            manga_text = item.text(0).lower()
            folder_text = item.text(1).lower()
            is_visible = filter_text in manga_text or filter_text in folder_text
            item.setHidden(not is_visible)

    def show_obras_context_menu(self, position):
        selected_items = self.obras_tree_widget.selectedItems()
        if not selected_items: return
        item = selected_items[0]
        data = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu()
        if len(selected_items) == 1:
            menu.addAction(get_icon("edit", AppTheme.TEXT_COLOR), "Editar...").triggered.connect(self.edit_selected_monitor_folder)

        provider_ids = [str(it.data(0, Qt.ItemDataRole.UserRole)['config'].get('manga_id'))
                        for it in selected_items
                        if 'provider' in it.data(0, Qt.ItemDataRole.UserRole)['config']
                        and it.data(0, Qt.ItemDataRole.UserRole)['config'].get('manga_id')]
        if provider_ids:
            texto_verif = (f"Verificar Agora ({len(provider_ids)} obras)"
                           if len(provider_ids) > 1 else "Verificar Agora")
            menu.addAction(get_icon("refresh-cw", AppTheme.TEXT_COLOR), texto_verif).triggered.connect(
                lambda: self.force_check_selected_obras(provider_ids))
        if len(selected_items) == 1 and 'provider' in data['config']:
            url_origem = data['config'].get('manga_url')
            if url_origem:
                menu.addAction(get_icon("external-link", AppTheme.TEXT_COLOR), "Abrir URL de Origem").triggered.connect(
                    lambda: QDesktopServices.openUrl(QUrl(url_origem)))

        action_text = f"Remover {len(selected_items)} Obra(s)" if len(selected_items) > 1 else "Remover Obra"
        menu.addAction(get_icon("trash-2", AppTheme.ERROR_COLOR), action_text).triggered.connect(self.remove_selected_monitor_folder)
        menu.addSeparator()
        move_menu = QMenu("Mover para outro Worker", self)
        move_menu.setIcon(get_icon("move", AppTheme.TEXT_COLOR))
        other_bots = [b_id for b_id in self.monitoramento_config if b_id != data['bot_id']]
        if not other_bots:
            move_menu.setEnabled(False)
        else:
            for target_bot_id in other_bots:
                move_menu.addAction(f"Worker {target_bot_id.replace('bot_', '')}").triggered.connect(lambda _, t_bot=target_bot_id: self.move_selected_mangas_to_bot(t_bot))
        menu.addMenu(move_menu)
        menu.exec(self.obras_tree_widget.viewport().mapToGlobal(position))

    def move_selected_mangas_to_bot(self, target_bot_id):
        selected_items = self.obras_tree_widget.selectedItems()
        if not selected_items: return
        moved_count = 0
        for item in selected_items:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            source_bot_id = data['bot_id']
            config_key = data['config_key']
            manga_config = data['config']
            if source_bot_id != target_bot_id:
                if source_bot_id in self.monitoramento_config and config_key in self.monitoramento_config[source_bot_id]:
                    del self.monitoramento_config[source_bot_id][config_key]
                if target_bot_id in self.monitoramento_config:
                    self.monitoramento_config[target_bot_id][config_key] = manga_config
                    moved_count += 1
        if moved_count > 0:
            self._save_monitoring_config()
            self.update_all_views()
            self.show_notification("Obras Movidas", f"{moved_count} obra(s) movida(s) para o '{target_bot_id}'.")

    def add_new_monitor_folder(self):
        bot_ids = list(self.monitoramento_config.keys())
        if not bot_ids:
            new_bot_id = self.add_new_bot()
            if not new_bot_id: return
            bot_ids.append(new_bot_id)
        dialog = AddMonitorDialog(self, self.api_client, bot_ids=bot_ids)
        if dialog.exec():
            result = dialog.get_results()
            manga_data = result['manga_data']
            manga_id = manga_data['id']
            config_key = ""
            new_config = result['config']
            new_config['manga_id'] = manga_id
            if result['type'] == 'folder':
                config_key = result['path']
                for b_config in self.monitoramento_config.values():
                    if config_key in b_config:
                        QMessageBox.warning(self, "Pasta Duplicada", "Esta pasta já está sendo monitorada.")
                        return
            else:
                provider_name = new_config.get('provider', 'generic')
                config_key = f"provider_{provider_name}_{manga_id}_{int(time.time())}"
            target_bot_id = result['target_bot_id']
            self.monitoramento_config[target_bot_id][config_key] = new_config
            self.manga_details_cache[str(manga_id)] = manga_data
            self._save_monitoring_config()
            self.update_all_views()
            QMessageBox.information(self, "Sucesso", f"Obra adicionada e atribuída ao '{target_bot_id}'.")

    def edit_selected_monitor_folder(self):
        selected_items = self.obras_tree_widget.selectedItems()
        if len(selected_items) != 1:
            QMessageBox.warning(self, "Seleção Inválida", "Por favor, selecione exatamente uma obra para editar.")
            return
        item = selected_items[0]
        data_for_edit = item.data(0, Qt.ItemDataRole.UserRole)
        old_bot_id = data_for_edit['bot_id']
        old_config_key = data_for_edit['config_key']
        old_config = data_for_edit['config']
        manga_id = old_config.get("manga_id")
        manga_data = self.manga_details_cache.get(str(manga_id))
        if not manga_data:
            QMessageBox.warning(self, "Aguarde", "Os detalhes desta obra ainda não foram carregados. Tente novamente.")
            return
        bot_ids = list(self.monitoramento_config.keys())
        dialog = AddMonitorDialog(self, self.api_client, bot_ids=bot_ids, current_bot_id=old_bot_id)
        dialog.set_data(data_for_edit, manga_data)
        if dialog.exec():
            result = dialog.get_results()
            new_bot_id = result['target_bot_id']
            new_manga_data = result['manga_data']
            new_manga_id = new_manga_data['id']
            new_config_key = ""
            new_config = result['config']
            new_config['manga_id'] = new_manga_id
            if result['type'] == 'folder':
                new_config_key = result['path']
                if new_config_key != old_config_key:
                    for bot_id, b_config in self.monitoramento_config.items():
                        if new_config_key in b_config:
                            QMessageBox.warning(self, "Pasta Duplicada", "Esta pasta já está sendo monitorada.")
                            return
            else: 
                old_is_provider = 'provider' in old_config
                if not old_is_provider or old_config.get('manga_url') != new_config.get('manga_url'):
                    provider_name = new_config.get('provider', 'generic')
                    new_config_key = f"provider_{provider_name}_{new_manga_id}_{int(time.time())}"
                else:
                    new_config_key = old_config_key
            if old_config_key in self.monitoramento_config[old_bot_id]:
                del self.monitoramento_config[old_bot_id][old_config_key]
            self.monitoramento_config[new_bot_id][new_config_key] = new_config
            self.manga_details_cache[str(new_manga_id)] = new_manga_data
            self._save_monitoring_config()
            self.update_all_views()
            QMessageBox.information(self, "Sucesso", "Configuração da obra atualizada com sucesso.")

    def remove_selected_monitor_folder(self):
        selected_items = self.obras_tree_widget.selectedItems()
        if not selected_items: return
        reply = QMessageBox.question(self, "Confirmar Remoção", f"Remover {len(selected_items)} obra(s) do monitoramento?")
        if reply == QMessageBox.StandardButton.Yes:
            for item in selected_items:
                data = item.data(0, Qt.ItemDataRole.UserRole)
                bot_id = data['bot_id']
                config_key = data['config_key']
                if bot_id in self.monitoramento_config and config_key in self.monitoramento_config[bot_id]:
                    del self.monitoramento_config[bot_id][config_key]
            self._save_monitoring_config()
            self.update_all_views()

    def populate_bots_list(self):
        self.bots_tree_widget.clear()
        for bot_id, bot_config in sorted(self.monitoramento_config.items()):
            is_running = bot_id in self.bot_processes and self.bot_processes[bot_id].state() == QProcess.ProcessState.Running
            status_text = "Rodando" if is_running else "Parado"
            status_color = QColor(AppTheme.SUCCESS_COLOR) if is_running else QColor(AppTheme.MUTED_TEXT_COLOR)
            num_obras = len([k for k in bot_config if not k.startswith('_')])
            item = QTreeWidgetItem([f"Worker {bot_id.replace('bot_', '')}", str(num_obras), status_text, ""])
            item.setForeground(2, status_color)
            item.setData(0, Qt.ItemDataRole.UserRole, bot_id)
            self.bots_tree_widget.addTopLevelItem(item)

    def on_bots_drop_event(self, event):
        source_widget = event.source()
        if source_widget is not self.obras_tree_widget:
            event.ignore()
            return
        target_item = self.bots_tree_widget.itemAt(event.pos())
        if not target_item:
            event.ignore()
            return
        target_bot_id = target_item.data(0, Qt.ItemDataRole.UserRole)
        dragged_items = source_widget.selectedItems()
        if not dragged_items:
            event.ignore()
            return
        self.move_selected_mangas_to_bot(target_bot_id)
        event.acceptProposedAction()

    def show_bots_management_context_menu(self, position):
        item = self.bots_tree_widget.itemAt(position)
        if not item: return
        bot_id = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu()        
        is_running = bot_id in self.bot_processes and self.bot_processes[bot_id].state() == QProcess.ProcessState.Running
        if is_running:
            menu.addAction(get_icon("square", AppTheme.ERROR_COLOR), "Parar Worker").triggered.connect(lambda: self.stop_bot(bot_id))
        else:
            menu.addAction(get_icon("play", AppTheme.SUCCESS_COLOR), "Iniciar Worker").triggered.connect(lambda: self.start_bot(bot_id))
        menu.addAction(get_icon("edit", AppTheme.TEXT_COLOR), "Renomear Worker...").triggered.connect(self.rename_selected_bot)
        menu.addSeparator()
        menu.addAction(get_icon("clock", AppTheme.TEXT_COLOR), "Definir Reinício Automático...").triggered.connect(lambda: self.set_bot_auto_restart(bot_id))
        menu.exec(self.bots_tree_widget.viewport().mapToGlobal(position))

    def set_bot_auto_restart(self, bot_id):
        if bot_id not in self.monitoramento_config: return
        settings = self.monitoramento_config[bot_id].get("_settings", {})
        current_interval = settings.get("restart_interval_hours", 0)
        new_interval, ok = QInputDialog.getInt(self, "Reinício Automático", f"Defina o intervalo em horas para o reinício automático do worker '{bot_id}'.\n(Use 0 para desativar)", current_interval, 0, 168, 1)
        if ok:
            if "_settings" not in self.monitoramento_config[bot_id]:
                self.monitoramento_config[bot_id]["_settings"] = {}
            self.monitoramento_config[bot_id]["_settings"]["restart_interval_hours"] = new_interval
            self._save_monitoring_config()
            self.log_manager_action(f"Intervalo de reinício para '{bot_id}' definido para {new_interval} horas.")
            self.update_bot_views()

    @Slot()
    def update_bot_countdowns(self):
        if not self.ui_initialized: return
        for i in range(self.bots_tree_widget.topLevelItemCount()):
            item = self.bots_tree_widget.topLevelItem(i)
            bot_id = item.data(0, Qt.ItemDataRole.UserRole)
            if bot_id not in self.bot_start_times:
                item.setText(3, "---")
                continue
            settings = self.monitoramento_config.get(bot_id, {}).get("_settings", {})
            interval_h = settings.get("restart_interval_hours", 0)
            if interval_h <= 0:
                item.setText(3, "Desativado")
                continue
            start_time = self.bot_start_times[bot_id]
            elapsed_seconds = time.time() - start_time
            total_seconds = interval_h * 3600
            remaining_seconds = total_seconds - elapsed_seconds
            if remaining_seconds <= 0:
                item.setText(3, "Reiniciando...")
                self.show_notification("Kaoryn: Reinício Automático", f"O worker '{bot_id}' está sendo reiniciado conforme agendado.")
                self.log_manager_action(f"Reiniciando worker '{bot_id}' automaticamente...")
                self.stop_bot(bot_id)
                QTimer.singleShot(2000, lambda b=bot_id: self.start_bot(b))
            else:
                h = int(remaining_seconds // 3600)
                m = int((remaining_seconds % 3600) // 60)
                s = int(remaining_seconds % 60)
                item.setText(3, f"{h:02d}:{m:02d}:{s:02d}")

    def add_new_bot(self):
        i = 1
        while f"bot_{i}" in self.monitoramento_config:
            i += 1
        new_bot_id = f"bot_{i}"
        self.monitoramento_config[new_bot_id] = {}
        self._save_monitoring_config()
        self.update_all_views()
        return new_bot_id

    def remove_selected_empty_bot(self):
        selected = self.bots_tree_widget.selectedItems()
        if not selected: return
        item = selected[0]
        bot_id = item.data(0, Qt.ItemDataRole.UserRole)
        if len([k for k in self.monitoramento_config.get(bot_id, {}) if not k.startswith('_')]) > 0:
            QMessageBox.warning(self, "Ação Inválida", "Apenas workers sem obras podem ser removidos. Mova as obras para outro worker primeiro.")
            return
        reply = QMessageBox.question(self, "Confirmar Remoção", f"Remover o worker '{bot_id}'?")
        if reply == QMessageBox.StandardButton.Yes:
            self.stop_bot(bot_id)
            if bot_id in self.monitoramento_config:
                del self.monitoramento_config[bot_id]
                self._save_monitoring_config()
                self.update_all_views()

    def export_config(self):
        config_path = self._get_config_path()
        if not config_path.exists():
            QMessageBox.warning(self, "Exportar", "Nenhum arquivo de configuração para exportar.")
            return
        save_path, _ = QFileDialog.getSaveFileName(self, "Salvar Configuração", "monitoramento_backup.json", "JSON Files (*.json)")
        if save_path:
            try:
                shutil.copy(config_path, save_path)
                QMessageBox.information(self, "Sucesso", f"Configuração exportada para:\n{save_path}")
            except Exception as e:
                QMessageBox.critical(self, "Erro", f"Não foi possível exportar a configuração: {e}")

    def import_config(self):
        load_path, _ = QFileDialog.getOpenFileName(self, "Importar Configuração", "", "JSON Files (*.json)")
        if not load_path: return
        try:
            with open(load_path, 'r', encoding='utf-8') as f:
                new_config = json.load(f)
            if not isinstance(new_config, dict) or not all(k.startswith('bot_') for k in new_config.keys()):
                raise ValueError("O arquivo não parece ser uma configuração de workers válida.")
        except Exception as e:
            QMessageBox.critical(self, "Erro de Importação", f"Não foi possível ler ou validar o arquivo de configuração:\n{e}")
            return
        reply = QMessageBox.question(self, "Confirmar Importação", "Isso irá parar todos os workers e substituir sua configuração atual. Deseja continuar?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.stop_all_bots()
            self.manga_details_cache.clear()
            self.monitoramento_config = new_config
            self._save_monitoring_config()
            self.update_all_views()
            QMessageBox.information(self, "Sucesso", "Configuração importada. Os workers foram parados e a lista foi atualizada.")

    def on_search_text_changed(self):
        self.search_timer.start()

    def refresh_manga_list(self, q=""):
        if not self.ui_initialized: return
        self.manga_list_widget.clear()
        self.manga_list_widget.addItem("Buscando obras...")
        self.manga_list_widget.setEnabled(False)
        self.search_input.setEnabled(False)
        worker = MangaListFetcher(self.api_client, q)
        worker.signals.finished.connect(self._on_manga_list_fetched)
        worker.signals.error.connect(self._on_manga_list_fetch_error)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_manga_list_fetched(self, task_id, payload):
        data = payload
        current_item = self.manga_list_widget.currentItem()
        current_id = current_item.data(Qt.ItemDataRole.UserRole)['id'] if current_item and current_item.data(Qt.ItemDataRole.UserRole) else None
        self.manga_list_widget.clear()
        
        new_selection_item = None
        mangas = sorted(data.get('results', []), key=lambda x: x.get('title', '').lower())
        for manga in mangas:
            item = QListWidgetItem(manga.get('title', ''))
            item.setData(Qt.ItemDataRole.UserRole, manga)
            self.manga_list_widget.addItem(item)
            if manga['id'] == current_id:
                new_selection_item = item
                
        if new_selection_item:
            self.manga_list_widget.setCurrentItem(new_selection_item)
        
        if not self.manga_list_widget.currentItem():
            self.on_manga_selection_changed(None)
        
        self.manga_list_widget.setEnabled(True)
        self.search_input.setEnabled(True)
        self.search_input.setFocus()

    @Slot(str, dict)
    def _on_manga_list_fetch_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido.')
        self.manga_list_widget.clear()
        self.manga_list_widget.addItem("Falha ao carregar obras.")
        QMessageBox.warning(self, "Erro de Rede", error_message)
        self.manga_list_widget.setEnabled(True)
        self.search_input.setEnabled(True)
        self.search_input.setFocus()

    def on_manga_selection_changed(self, current_item):
        self.display_manga_details(current_item)

    def display_manga_details(self, item):
        while self.manga_details_layout.count():
            child = self.manga_details_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if not item:
            placeholder = QWidget()
            layout = QVBoxLayout(placeholder)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(QLabel("Selecione uma obra na lista à esquerda."))
            self.manga_details_layout.addWidget(placeholder)
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            self.display_manga_details(None)
            return
        details_widget = QWidget()
        layout = QVBoxLayout(details_widget)
        layout.setSpacing(15)
        
        title = QLabel(data.get('title', 'Título Desconhecido'))
        title.setObjectName("TitleLabel")
        title.setWordWrap(True)
        layout.addWidget(title)
        
        info_layout = QFormLayout()
        info_layout.setSpacing(10)
        info_layout.addRow("ID:", QLabel(str(data.get('id', 'N/A'))))
        info_layout.addRow("Status:", QLabel(data.get('status', '').capitalize()))
        self.total_chapters_label = QLabel("<i>Carregando...</i>")
        self.last_chapter_label = QLabel("<i>Carregando...</i>")
        info_layout.addRow("Total de Capítulos:", self.total_chapters_label)
        info_layout.addRow("Último Capítulo:", self.last_chapter_label)
        layout.addLayout(info_layout)
        
        actions_layout = QHBoxLayout()
        self.download_manga_button = QPushButton(" Baixar Obra Completa")
        self.download_manga_button.setIcon(get_icon("download-cloud", AppTheme.TEXT_COLOR))
        self.download_manga_button.clicked.connect(self.start_download_from_details)
        self.download_manga_button.setEnabled(False)
        actions_layout.addWidget(self.download_manga_button)
        
        edit_manga_button = QPushButton(" Editar Obra")
        edit_manga_button.setIcon(get_icon("edit", AppTheme.TEXT_COLOR))
        edit_manga_button.clicked.connect(lambda: self.handle_edit_manga(data))
        actions_layout.addWidget(edit_manga_button)

        open_site_button = QPushButton(" Abrir no Site")
        open_site_button.setIcon(get_icon("external-link", AppTheme.TEXT_COLOR))
        open_site_button.setEnabled(bool(data.get('slug')))
        open_site_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(f"https://nexustoons.com/manga/{data.get('slug')}")))
        actions_layout.addWidget(open_site_button)

        actions_layout.addStretch()
        layout.addLayout(actions_layout)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        chapters_label = QLabel("Capítulos Publicados")
        chapters_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(chapters_label)
        self.chapters_online_tree = QTreeWidget()
        self.chapters_online_tree.setHeaderLabels(["Número", "Título"])
        self.chapters_online_tree.setRootIsDecorated(False)
        self.chapters_online_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.chapters_online_tree.setMaximumHeight(220)
        self.chapters_online_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.chapters_online_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.chapters_online_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chapters_online_tree.customContextMenuRequested.connect(self.show_online_chapters_context_menu)
        placeholder_item = QTreeWidgetItem(["Carregando...", ""])
        self.chapters_online_tree.addTopLevelItem(placeholder_item)
        layout.addWidget(self.chapters_online_tree)

        upload_label = QLabel("Upload Manual de Capítulos")
        upload_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(upload_label)
        self.drop_area = DropArea()
        self.drop_area.filesDropped.connect(self.handle_files_dropped)
        layout.addWidget(self.drop_area)
        layout.addStretch()
        self.manga_details_layout.addWidget(details_widget)
        
        worker = ChapterDetailsFetcher(self.api_client, data['id'])
        worker.signals.finished.connect(self._on_chapters_fetched)
        worker.signals.error.connect(self._on_chapters_fetch_error)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_chapters_fetched(self, task_id, payload):
        manga_id_str = payload.get('manga_id')
        chapters = payload.get('chapters', [])
        
        current_item = self.manga_list_widget.currentItem()
        if not current_item or str(current_item.data(Qt.ItemDataRole.UserRole)['id']) != manga_id_str:
            return
        self.latest_chapters_online[manga_id_str] = {str(c.get('number')) for c in chapters}
        self.total_chapters_label.setText(f"<b>{len(chapters)}</b>")
        self.chapters_online_tree.clear()
        if chapters:
            self.download_manga_button.setEnabled(True)
            try:
                sorted_chapters = sorted(chapters, key=lambda x: natural_sort_key(x.get('number', '0')))
                self.last_chapter_label.setText(f"<b>{sorted_chapters[-1]['number']}</b>")
            except (IndexError, KeyError):
                sorted_chapters = chapters
                self.last_chapter_label.setText("<font color='orange'>Inválido</font>")
            for c in reversed(sorted_chapters):
                cap_item = QTreeWidgetItem([str(c.get('number', '?')), c.get('title') or ""])
                cap_item.setData(0, Qt.ItemDataRole.UserRole, c)
                self.chapters_online_tree.addTopLevelItem(cap_item)
        else:
            self.last_chapter_label.setText("Nenhum")

    @Slot(str, dict)
    def _on_chapters_fetch_error(self, task_id, payload):
        manga_id_str = payload.get('manga_id')
        error_message = payload.get('message', 'Erro desconhecido.')

        current_item = self.manga_list_widget.currentItem()
        if not current_item or str(current_item.data(Qt.ItemDataRole.UserRole)['id']) != manga_id_str:
            return
        error_text = f"<font color='{AppTheme.ERROR_COLOR}'>Falha ao buscar</font>"
        self.total_chapters_label.setText(error_text)
        self.last_chapter_label.setText(error_text)

    def show_online_chapters_context_menu(self, position):
        selecionados = [it for it in self.chapters_online_tree.selectedItems()
                        if it.data(0, Qt.ItemDataRole.UserRole)]
        if not selecionados:
            return
        current_item = self.manga_list_widget.currentItem()
        if not current_item:
            return
        manga_data = current_item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu()

        if len(selecionados) == 1 and manga_data.get('slug'):
            cap = selecionados[0].data(0, Qt.ItemDataRole.UserRole)
            url_leitor = f"https://nexustoons.com/ler/{manga_data['slug']}/{cap.get('id')}"
            menu.addAction(get_icon("external-link", AppTheme.TEXT_COLOR), "Abrir no Leitor").triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl(url_leitor)))

        rotulo = (f"Apagar {len(selecionados)} Capítulos do Site"
                  if len(selecionados) > 1 else "Apagar Capítulo do Site")
        menu.addAction(get_icon("trash-2", AppTheme.ERROR_COLOR), rotulo).triggered.connect(
            lambda: self.delete_online_chapters(manga_data, [it.data(0, Qt.ItemDataRole.UserRole) for it in selecionados]))
        menu.exec(self.chapters_online_tree.viewport().mapToGlobal(position))

    def delete_online_chapters(self, manga_data, chapters):
        numeros = ", ".join(str(c.get('number', '?')) for c in chapters)
        reply = QMessageBox.warning(
            self, "Apagar Capítulos do Site",
            f"Apagar do site o(s) capítulo(s) <b>{numeros}</b> de '<i>{manga_data.get('title')}</i>'?\n\n"
            "O registro sai da API e as páginas são removidas do R2. Não tem volta.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.chapters_online_tree.setEnabled(False)
        self.status_bar.showMessage(f"Apagando {len(chapters)} capítulo(s) do site...", 0)
        worker = ChapterDeleteWorker(self.api_client, self.s3_handler, manga_data['id'], chapters)
        worker.signals.finished.connect(self._on_online_chapters_deleted)
        self.thread_pool.start(worker)

    @Slot(str, dict)
    def _on_online_chapters_deleted(self, task_id, payload):
        self.chapters_online_tree.setEnabled(True)
        apagados = payload.get('apagados', [])
        falhas = payload.get('falhas', [])
        self.status_bar.showMessage(f"{len(apagados)} capítulo(s) apagado(s) do site.", 5000)
        if falhas:
            QMessageBox.warning(self, "Falhas ao Apagar",
                                "Alguns capítulos não foram apagados:\n" + "\n".join(falhas))
        # remove do cache de duplicados e recarrega a lista da obra
        manga_id_str = payload.get('manga_id')
        if manga_id_str in self.latest_chapters_online:
            self.latest_chapters_online[manga_id_str] -= set(apagados)
        current_item = self.manga_list_widget.currentItem()
        if current_item and str(current_item.data(Qt.ItemDataRole.UserRole)['id']) == manga_id_str:
            worker = ChapterDetailsFetcher(self.api_client, int(manga_id_str))
            worker.signals.finished.connect(self._on_chapters_fetched)
            worker.signals.error.connect(self._on_chapters_fetch_error)
            self.thread_pool.start(worker)

    def start_download_from_details(self):
        current_item = self.manga_list_widget.currentItem()
        if not current_item: return
        manga_data = current_item.data(Qt.ItemDataRole.UserRole)
        manga_id = manga_data.get('id')
        manga_title = manga_data.get('title', f"Obra ID {manga_id}")
        dest_path = QFileDialog.getExistingDirectory(self, "Selecione a Pasta de Destino para Baixar a Obra")
        if not dest_path: return
        QMessageBox.information(self, "Download Iniciado", f"O download de '{manga_title}' foi iniciado.\nVocê pode acompanhar o progresso na aba 'Fila de Tarefas'.")
        task_id = f"download-{manga_id}-{time.time()}"
        chapter_config_mock = {'task_id': task_id, 'path': manga_title, 'manga_title': manga_title}
        self.add_task_to_manager(manga_id, manga_title, chapter_config_mock, is_download=True)
        worker = MangaDownloadWorker(task_id, self.api_client, manga_id, manga_title, dest_path)
        worker.signals.finished.connect(self._on_task_finished)
        worker.signals.error.connect(self._on_task_error)
        worker.signals.progress.connect(self.update_task_progress)
        worker.signals.status_update.connect(self.update_task_status)
        self.running_workers[task_id] = worker
        self.thread_pool.start(worker)

    def handle_files_dropped(self, paths):
        if not self.manga_list_widget.currentItem():
            QMessageBox.warning(self, "Nenhuma Obra Selecionada", "Por favor, selecione uma obra na lista antes de arrastar os capítulos.")
            return
        self.drop_area.setText("Verificando pastas...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        verification_worker = ChapterVerificationWorker(paths)
        verification_worker.signals.finished.connect(self._on_chapters_verified)
        verification_worker.signals.error.connect(self._on_chapter_verification_error)
        self.thread_pool.start(verification_worker)

    @Slot(str, dict)
    def _on_chapters_verified(self, task_id, payload):
        QApplication.restoreOverrideCursor()
        self.drop_area.setText("\n\nArraste as Pastas dos Capítulos Aqui\n\n")
        valid_chapters, all_warnings = payload.get('result', ({}, []))
        if all_warnings:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setWindowTitle("Relatório de Verificação")
            msg_box.setText("<b>Atenção:</b> Alguns arquivos ou pastas foram ignorados.")
            msg_box.setInformativeText("<br><br>".join(all_warnings))
            msg_box.exec()
        if not valid_chapters: return
        sorted_chapters = dict(sorted(valid_chapters.items(), key=lambda item: natural_sort_key(os.path.basename(item[0]))))
        dialog = UploadStagingDialog(sorted_chapters, self)
        if dialog.exec():
            selected_chapters_to_upload = dialog.get_selected_chapters_config()
            if selected_chapters_to_upload:
                manga_data = self.manga_list_widget.currentItem().data(Qt.ItemDataRole.UserRole)
                manga_id = manga_data['id']
                manga_title = manga_data['title']

                self.status_bar.showMessage(f"Preparando {len(selected_chapters_to_upload)} capítulos para a fila...", 3000)
                
                enqueuing_worker = TaskEnqueuingWorker(selected_chapters_to_upload, manga_id, manga_title)
                enqueuing_worker.signals.finished.connect(self._on_tasks_ready_for_queue)
                enqueuing_worker.signals.error.connect(self._on_enqueue_error)
                self.thread_pool.start(enqueuing_worker)


    @Slot(str, dict)
    def _on_tasks_ready_for_queue(self, task_id, payload):
        tasks_to_enqueue = payload.get('tasks', [])
        manga_id = payload.get('manga_id')
        
        if not tasks_to_enqueue or not manga_id:
            return
        manga_title = tasks_to_enqueue[0].get('manga_title', 'Título Desconhecido')
        self.start_processing_and_upload(tasks_to_enqueue, manga_id, manga_title) 
        self.status_bar.showMessage(f"{len(tasks_to_enqueue)} capítulos adicionados à fila.", 3000)


    @Slot(str, dict)
    def _on_enqueue_error(self, task_id, payload):
        error_message = payload.get('message', 'Erro desconhecido ao preparar tarefas.')
        QMessageBox.critical(self, "Erro de Enfileiramento", error_message)

    @Slot(str, dict)
    def _on_chapter_verification_error(self, task_id, payload):
        QApplication.restoreOverrideCursor()
        self.drop_area.setText("\n\nArraste as Pastas dos Capítulos Aqui\n\n")
        error_message = payload.get('message', 'Erro desconhecido na verificação')
        QMessageBox.critical(self, "Erro na Verificação", str(error_message))

    @Slot(int)
    def on_manga_concurrency_changed(self, value):
        self.MAX_CONCURRENT_MANGAS = value
        self.settings_manager.set("manga_concurrency", value)
        self.settings_manager.save_settings()
        self.schedule_next_tasks()

    @Slot(int)
    def on_chapter_concurrency_changed(self, value):
        self.MAX_CHAPTERS_PER_MANGA = value
        self.settings_manager.set("chapter_concurrency", value)
        self.settings_manager.save_settings()
        self.schedule_next_tasks()

    def clear_finished_tasks(self):
        root = self.tasks_tree.invisibleRootItem()
        for manga_id in list(self.task_widgets.keys()):
            manga_dict = self.task_widgets[manga_id]
            tasks_to_remove = [task_id for task_id, data in manga_dict['chapters'].items() if data.get('final_status') in ['success', 'cancelled']]
            for task_id in tasks_to_remove:
                item_to_remove = manga_dict['chapters'][task_id]['item']
                manga_dict['manga_item'].removeChild(item_to_remove)
                del manga_dict['chapters'][task_id]
                self.task_to_manga_map.pop(task_id, None)
                self.task_configs.pop(task_id, None)
                self.finished_task_ids.discard(task_id)
            if manga_dict['manga_item'].childCount() == 0:
                root.removeChild(manga_dict['manga_item'])
                del self.task_widgets[manga_id]

    def add_task_to_manager(self, manga_id, manga_title, chapter_config, bot_id=None, is_download=False):
        task_id = chapter_config['task_id']
        self.task_configs[task_id] = chapter_config
        self.task_to_manga_map[task_id] = manga_id
        if manga_id not in self.task_widgets:
            manga_item = QTreeWidgetItem(self.tasks_tree, [manga_title])
            manga_item.setExpanded(True)
            self.task_widgets[manga_id] = {'manga_item': manga_item, 'chapters': {}}
        manga_dict = self.task_widgets[manga_id]
        
        if is_download:
            display_name = chapter_config.get('manga_title', 'Download de Obra')
        else:
            chapter_number = chapter_config.get('number', 'N/A')
            display_name = f"Capítulo {chapter_number}"
        
        chapter_item = QTreeWidgetItem()
        chapter_item.setData(0, Qt.ItemDataRole.UserRole, task_id)
        if is_download:
            chapter_item.setText(0, f" [DOWNLOAD] {display_name}")
            chapter_item.setIcon(0, get_icon("download", AppTheme.TEXT_COLOR))
        elif bot_id:
            bot_display = f"Worker {bot_id.replace('bot_', '')}"
            chapter_item.setText(0, f" [{bot_display.upper()}] {display_name}")
            chapter_item.setIcon(0, get_icon("cpu", AppTheme.TEXT_COLOR))
        else:
            manual_display_name = os.path.basename(chapter_config.get('path', ''))
            chapter_item.setText(0, f" {manual_display_name} ({display_name})")
            chapter_item.setIcon(0, get_icon("user", AppTheme.TEXT_COLOR))
        
        manga_dict['manga_item'].addChild(chapter_item)
        
        if chapter_config.get('scheduled_time'):
            scheduled_time = datetime.fromisoformat(chapter_config['scheduled_time'])
            status_text = f"🕒 Agendado para {scheduled_time.strftime('%d/%m %H:%M')}"
            chapter_item.setText(1, status_text)
            chapter_item.setForeground(1, QColor(AppTheme.MUTED_TEXT_COLOR))
            manga_dict['chapters'][task_id] = {'item': chapter_item, 'final_status': None}
        else:
            progress_bar = QProgressBar()
            progress_bar.setRange(0, 100)
            progress_bar.setValue(0)
            progress_bar.setTextVisible(True)
            progress_bar.setFormat("%p% - Pronta")
            self.tasks_tree.setItemWidget(chapter_item, 1, progress_bar)
            manga_dict['chapters'][task_id] = {'item': chapter_item, 'progress_bar': progress_bar, 'final_status': None}
            
    def _handle_cancel_request(self, task_id):
        if task_id in self.running_workers:
            worker = self.running_workers[task_id]
            worker.cancel()
        else:
            manga_id = self.task_to_manga_map.get(task_id)
            if manga_id and manga_id in self.manga_task_queues:
                queue = self.manga_task_queues[manga_id]
                tasks_to_keep = deque()
                task_removed = False
                for t in queue:
                    if t['task_id'] == task_id:
                        task_removed = True
                    else:
                        tasks_to_keep.append(t)
                if task_removed:
                    self.manga_task_queues[manga_id] = tasks_to_keep
                    self.task_status_update_requested.emit(task_id, "Removido da Fila", True)

    @Slot(str, int, str)
    def update_task_progress(self, task_id, value, message=None):
        self.progress_update_buffer[task_id] = (value, message)
        
        if not self.progress_update_timer.isActive():
            self.progress_update_timer.start()
    
    @Slot()
    def _process_progress_updates(self):
        updates_to_process = self.progress_update_buffer
        self.progress_update_buffer = {}

        for task_id, (value, message) in updates_to_process.items():
            manga_id = self.task_to_manga_map.get(task_id)
            
            if not manga_id or manga_id not in self.task_widgets:
                continue
            
            chapter_data = self.task_widgets[manga_id]['chapters'].get(task_id)
            if not chapter_data or chapter_data.get('final_status'):
                continue
            
            progress_bar = chapter_data.get('progress_bar')
            if not progress_bar:
                continue

            try:
                if value is not None and progress_bar.value() != value:
                    progress_bar.setValue(value)
                
                if message:
                    progress_bar.setFormat(f"%p% - {message}")
            except RuntimeError:
                pass

    @Slot(str, str)
    def update_task_status(self, task_id, message):
        manga_id = self.task_to_manga_map.get(task_id)
        if manga_id and manga_id in self.task_widgets:
            chapter_data = self.task_widgets[manga_id]['chapters'].get(task_id)
            if chapter_data and not chapter_data.get('final_status'):
                item = chapter_data['item']
                if self.tasks_tree.itemWidget(item, 1):
                    self.tasks_tree.itemWidget(item, 1).setFormat(f"%p% - {message}")
                else:
                    item.setText(1, message)

    def retry_failed_task(self, task_id):
        if task_id not in self.task_configs: return
        manga_id = self.task_to_manga_map.get(task_id)
        if manga_id not in self.task_widgets or task_id not in self.task_widgets[manga_id]['chapters']: return
        
        manga_item = self.task_widgets[manga_id]['manga_item']
        chapter_item = self.task_widgets[manga_id]['chapters'][task_id]['item']
        manga_item.removeChild(chapter_item)
        del self.task_widgets[manga_id]['chapters'][task_id]

        self.finished_task_ids.discard(task_id)
        
        new_config = self.task_configs[task_id].copy()
        new_config['retries'] = new_config.get('retries', 0) + 1
        
        if manga_id not in self.manga_task_queues:
            self.manga_task_queues[manga_id] = deque()
        self.manga_task_queues[manga_id].appendleft(new_config)
        if manga_id not in self.manga_queue_order:
            self.manga_queue_order.appendleft(manga_id)
        
        self.add_task_to_manager(manga_id, new_config['manga_title'], new_config)
        self.schedule_next_tasks()

    def show_task_context_menu(self, position):
        item = self.tasks_tree.itemAt(position)
        if not item or not item.parent(): return
        task_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not task_id: return
        
        manga_id = self.task_to_manga_map.get(task_id, '')
        if not manga_id or task_id not in self.task_widgets[manga_id]['chapters']: return

        chapter_data = self.task_widgets[manga_id]['chapters'][task_id]
        status = chapter_data.get('final_status')
        menu = QMenu()
        
        if status == 'error':
            menu.addAction(get_icon("refresh-cw", AppTheme.TEXT_COLOR), "Tentar Novamente").triggered.connect(lambda: self.retry_failed_task(task_id))
        if status is None:
            menu.addAction(get_icon("x-circle", AppTheme.ERROR_COLOR), "Cancelar").triggered.connect(lambda: self._handle_cancel_request(task_id))
        
        if menu.actions():
            menu.exec(self.tasks_tree.viewport().mapToGlobal(position))

    def on_tasks_drag_move(self, event):
        item = self.tasks_tree.itemAt(event.pos())
        if item and item.parent():
            event.accept()
        else:
            event.ignore()

    def on_tasks_drop_event(self, event):
        source_item = self.tasks_tree.currentItem()
        if not source_item or not source_item.parent():
            event.ignore()
            return
        target_item = self.tasks_tree.itemAt(event.pos())
        if not target_item or not target_item.parent() or source_item is target_item:
            event.ignore()
            return
        
        source_task_id = source_item.data(0, Qt.ItemDataRole.UserRole)
        target_task_id = target_item.data(0, Qt.ItemDataRole.UserRole)
        source_manga_id = self.task_to_manga_map.get(source_task_id)
        target_manga_id = self.task_to_manga_map.get(target_task_id)

        if source_manga_id != target_manga_id:
            QMessageBox.warning(self, "Ação Inválida", "Só é possível reordenar capítulos da mesma obra.")
            event.ignore()
            return
        
        queue = self.manga_task_queues.get(source_manga_id)
        if not queue:
            event.ignore()
            return
        
        source_config = self.task_configs.get(source_task_id)
        if not source_config or source_config not in queue:
            QMessageBox.warning(self, "Ação Inválida", "Não é possível reordenar uma tarefa que já foi iniciada ou concluída.")
            event.ignore()
            return

        try:
            queue.remove(source_config)
            target_index = -1
            for i, task_config in enumerate(queue):
                if task_config['task_id'] == target_task_id:
                    target_index = i
                    break
            if target_index != -1:
                queue.insert(target_index, source_config)
            else:
                queue.append(source_config)
        except ValueError:
            event.ignore()
            return

        parent_item = source_item.parent()
        parent_item.removeChild(source_item)
        target_idx_visual = parent_item.indexOfChild(target_item)
        parent_item.insertChild(target_idx_visual, source_item)
        self.tasks_tree.setCurrentItem(source_item)
        event.accept()

    def _on_tab_changed(self, index):
        if self.tab_widget.widget(index) == self.dashboard_tab:
            self.update_dashboard()

    def update_dashboard(self):
        if not self.ui_initialized: return
        summary = self.stats_manager.get_stats_summary()
        self.uploads_today_label_widget.findChild(QLabel, "valueLabel").setText(str(summary["today"]))
        self.uploads_week_label_widget.findChild(QLabel, "valueLabel").setText(str(summary["week"]))
        self.uploads_total_label_widget.findChild(QLabel, "valueLabel").setText(str(summary["total"]))
        if pg:
            last_7_days_data = self.stats_manager.get_last_7_days_data()
            days = list(last_7_days_data.keys())
            uploads = list(last_7_days_data.values())
            x_ticks = list(enumerate(days))
            if self.bar_chart is None:
                self.plot_widget.disableAutoRange(axis=pg.ViewBox.YAxis)
                self.bar_chart = pg.BarGraphItem(x=range(len(uploads)), height=uploads, width=0.6, brush=AppTheme.ACCENT_COLOR)
                self.plot_widget.addItem(self.bar_chart)
            else:
                self.bar_chart.setOpts(x=range(len(uploads)), height=uploads)
            max_y_value = max(max(uploads) if uploads else 0, 5)
            self.plot_widget.setYRange(-0.5, max_y_value + 1)
            self.plot_widget.setXRange(-0.5, len(days) - 0.5)
            axis = self.plot_widget.getAxis('bottom')
            axis.setTicks([x_ticks])

if __name__ == "__main__":
    os.chdir(base_path)

    if os.name == 'nt':
        os.environ['QT_FONT_DPI'] = '96'

    app = QApplication(sys.argv)
    AppTheme.apply(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())