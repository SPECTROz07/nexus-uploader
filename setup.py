# setup.py (Versão Final v1.0.6 - Múltiplos Executáveis com Ícones Específicos)

import sys
from cx_Freeze import setup, Executable

packages_to_include = [
    'PySide6',
    'PySide6.QtSvg',
    'multiprocessing',
    'psutil',
    'requests',
    'cloudscraper',
    'urllib3',
    'bs4',
    'selenium',
    'playwright',
    'undetected_chromedriver',
    'nodriver',
    'pyqtgraph',
    'numpy',
    'OpenGL',
    'PIL',
    'pillow_heif',
    'boto3',
    'botocore',
    'watchdog',
    'tldextract'
]

build_options = {
    'packages': packages_to_include,
    'excludes': [
        'tkinter',
        'unittest',
        'xmlrpc',
        'matplotlib',
        'scipy',
        'torch',
        'tensorflow',
        'PyInstaller'
    ],
    'include_files': [
        ('assets', 'assets'),
        ('ui', 'ui'),
        ('core', 'core'),
        ('providers', 'providers'),
        ('providers_obra', 'providers_obra'),
        ('playwright_browsers', 'playwright_browsers')
    ]
}

base = 'Win32GUI' if sys.platform == 'win32' else None

executables = [
    Executable(
        script='main_gui.py',
        base=base,
        target_name='NexusUploader.exe',
        icon='assets/logo.ico'
    ),
    Executable(
        script='run_bot.py',
        base=None,
        target_name='run_bot_console.exe'
    ),
    Executable(
        script='mass_importer_gui.py',
        base=base,
        target_name='MassImporter.exe',
        icon='assets/icon.png'  # Ícone específico para o importador
    )
]

setup(
    name='Nexus Hybrid Uploader',
    version='1.0.6',
    description='Nexus Hybrid Uploader - Manga Upload Tool',
    options={'build_exe': build_options},
    executables=executables
)