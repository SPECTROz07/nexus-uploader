# --- START OF FILE ui/icons.py ---

import os
import sys
from pathlib import Path
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import QSize, Qt

_icon_cache = {}
_ICON_ROOT = None

def get_icon_path():
    """Determina o caminho da pasta de ícones."""
    global _ICON_ROOT
    if _ICON_ROOT is None:
        if getattr(sys, 'frozen', False):
            base_path = Path(sys.executable).parent
        else:
            base_path = Path(__file__).resolve().parents[1] 
        _ICON_ROOT = base_path / "assets" / "icons"
    return _ICON_ROOT

def get_icon(name: str, color: str = "#ffffff") -> QIcon:
    """
    Carrega um ícone SVG do sistema de arquivos, colore e o retorna como QIcon.

    Args:
        name: O nome do arquivo do ícone (sem a extensão .svg).
        color: A cor para aplicar ao ícone (ex: "#ffffff").

    Returns:
        Um objeto QIcon.
    """
    cache_key = (name, color)
    if cache_key in _icon_cache:
        return _icon_cache[cache_key]

    icon_path = get_icon_path() / f"{name}.svg"

    if not icon_path.exists():
        print(f"AVISO: Ícone não encontrado: {icon_path}")
        return QIcon() # Retorna um ícone vazio

    with open(icon_path, 'r', encoding='utf-8') as f:
        svg_data = f.read()

    colored_svg = svg_data.replace('currentColor', color)
    renderer = QSvgRenderer(colored_svg.encode('utf-8'))
    size = renderer.defaultSize()
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent) 
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()

    icon = QIcon(pixmap)
    _icon_cache[cache_key] = icon
    return icon