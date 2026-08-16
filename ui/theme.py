# --- START OF FILE ui/theme.py ---

class AppTheme:
    # Paleta de Cores Inspirada no Spotify/Discord
    PRIMARY_COLOR = "#121212"    # Fundo principal da janela
    SECONDARY_COLOR = "#181818"  # Fundo de painéis laterais
    TERTIARY_COLOR = "#282828"   # Fundo de inputs, listas, cards
    HOVER_COLOR = "#383838"      # Cor ao passar o mouse

    ACCENT_COLOR = "#FF0400"   
    ACCENT_HOVER_COLOR = "#FF1100"

    TEXT_COLOR = "#FFFFFF"
    MUTED_TEXT_COLOR = "#B3B3B3"
    SUCCESS_COLOR = "#4CAF50"
    ERROR_COLOR = "#F44336"
    
    # Adicionando transições suaves e um visual mais limpo
    STYLESHEET = f"""
        QWidget {{
            background-color: {SECONDARY_COLOR};
            color: {TEXT_COLOR};
            border: none;
            font-family: Segoe UI, Roboto, sans-serif;
            font-size: 10pt;
        }}
        QMainWindow {{
            background-color: {PRIMARY_COLOR};
        }}

        /* --- Textos e Títulos --- */
        QLabel#TitleLabel {{
            font-size: 18pt;
            font-weight: bold;
            padding: 5px;
        }}
        QLabel {{
            padding: 2px;
        }}

        /* --- Inputs e ComboBoxes --- */
        QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QDateTimeEdit, QTimeEdit {{
            background-color: {TERTIARY_COLOR};
            border: 1px solid #404040;
            padding: 8px 10px;
            border-radius: 6px;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QDateTimeEdit:focus, QTimeEdit:focus {{
            border: 1px solid {ACCENT_COLOR};
        }}
        QComboBox::drop-down {{
            border: none;
        }}
        QComboBox::down-arrow {{
            image: url(assets/icons/chevron-down.svg); /* Adicione um ícone se quiser */
            width: 14px;
            height: 14px;
        }}

        /* --- Botões --- */
        QPushButton {{
            background-color: {TERTIARY_COLOR};
            padding: 9px 18px;
            border-radius: 6px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: {HOVER_COLOR};
        }}
        QPushButton:pressed {{
            background-color: #484848;
        }}
        QPushButton#PrimaryButton {{
            background-color: {ACCENT_COLOR};
            color: white;
        }}
        QPushButton#PrimaryButton:hover {{
            background-color: {ACCENT_HOVER_COLOR};
        }}

        /* --- Listas e Árvores --- */
        QListWidget, QTreeWidget {{
            background-color: {TERTIARY_COLOR};
            border-radius: 6px;
            padding: 5px;
        }}
        QListWidget::item, QTreeWidget::item {{
            padding: 12px;
            margin: 2px 0;
            border-radius: 4px;
        }}
        QListWidget::item:hover, QTreeWidget::item:hover {{
            background-color: {SECONDARY_COLOR};
        }}
        QListWidget::item:selected, QTreeWidget::item:selected {{
            background-color: {ACCENT_COLOR};
            color: white;
        }}
        QHeaderView::section {{
            background-color: {PRIMARY_COLOR};
            padding: 10px;
            font-weight: bold;
            border: none;
            border-bottom: 1px solid #404040;
        }}
        QTreeWidget::branch {{
            background-color: transparent;
        }}
        
        /* --- Abas --- */
        QTabBar::tab {{
            background: transparent;
            padding: 10px 20px;
            border: none;
            border-bottom: 2px solid transparent;
            font-weight: bold;
            color: {MUTED_TEXT_COLOR};
        }}
        QTabBar::tab:selected {{
            color: {TEXT_COLOR};
            border-bottom: 2px solid {ACCENT_COLOR};
        }}
        QTabBar::tab:!selected:hover {{
            color: {TEXT_COLOR};
        }}
        QTabWidget::pane {{
            border: none;
        }}

        /* --- Barras de Progresso e Status --- */
        QStatusBar {{
            background-color: {PRIMARY_COLOR};
            font-weight: bold;
        }}
        QProgressBar {{
            border: 1px solid {TERTIARY_COLOR};
            border-radius: 8px;
            text-align: center;
            font-weight: bold;
            color: {TEXT_COLOR};
            background-color: {TERTIARY_COLOR};
        }}
        QProgressBar::chunk {{
            background-color: {ACCENT_COLOR};
            border-radius: 7px;
        }}
        
        /* --- Outros --- */
        QFrame[frameShape="4"], QFrame[frameShape="5"] {{ /* HLine, VLine */
            background-color: #404040;
        }}
        QPlainTextEdit#LogBox {{
            font-family: Consolas, Courier New, monospace;
            background-color: {PRIMARY_COLOR};
            border-radius: 6px;
        }}
        #StatsCard {{
            background-color: {TERTIARY_COLOR};
            border-radius: 8px;
            padding: 15px;
        }}
    """
    @staticmethod
    def apply(app):
        app.setStyle("Fusion")
        app.setStyleSheet(AppTheme.STYLESHEET)