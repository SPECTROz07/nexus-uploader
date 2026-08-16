# setup_dist.py — build de DISTRIBUIÇÃO (código fechado) via cx_Freeze.
#
# Diferente do setup.py (dev): aqui core/ e ui/ NÃO são copiados como .py — vão
# CONGELADOS como bytecode no library.zip do exe. Os providers entram como .pyd
# EXTERNOS (de dist_providers/), pra continuarem atualizáveis pelo repo. Assim o
# uploader recebe um NexusUploader.exe dependente de pasta (abre rápido, sem
# precisar de Python) e sem o código-fonte legível.
#
# Rodar de F:\HybridUpload:  venv\Scripts\python.exe setup_dist.py build
import sys
from cx_Freeze import setup, Executable

packages_to_include = [
    'PySide6', 'PySide6.QtSvg', 'multiprocessing', 'psutil', 'requests',
    'cloudscraper', 'urllib3', 'bs4', 'selenium', 'playwright',
    'undetected_chromedriver', 'nodriver', 'pyqtgraph', 'numpy', 'OpenGL',
    'PIL', 'pillow_heif', 'boto3', 'botocore', 'watchdog', 'tldextract',
    # libs que só os providers usam (carregados dinamicamente, não escaneados):
    'curl_cffi', 'playwright_stealth', 'cryptography',
    # nosso código, congelado como bytecode (NÃO copiado como .py):
    'core', 'ui',
]

build_options = {
    'packages': packages_to_include,
    'excludes': [
        'tkinter', 'unittest', 'xmlrpc', 'matplotlib', 'scipy', 'torch',
        'tensorflow', 'PyInstaller',
        # fora do escopo / dependências ausentes:
        'core.orion_wp_client',   # outro programa (WordPress/Orion)
        'core.get_token',         # utilitário de dev
        'core.storage_handler',   # legado (Bunny/pysftp não instalado)
    ],
    'include_files': [
        ('assets', 'assets'),
        ('dist_providers/providers', 'providers'),            # .pyd + manifest
        ('dist_providers/providers_obra', 'providers_obra'),  # .pyd + manifest
        ('playwright_browsers', 'playwright_browsers'),
        ('uploaders.json', 'uploaders.json'),                 # allowlist (vazia)
        ('s3_config.example.json', 's3_config.example.json'),
        ('settings.example.json', 'settings.example.json'),
    ],
    'build_exe': 'build/NexusUploader',
}

base = 'Win32GUI' if sys.platform == 'win32' else None

executables = [
    Executable(script='main_gui.py', base=base,
               target_name='NexusUploader.exe', icon='assets/logo.ico'),
    Executable(script='run_bot.py', base=None, target_name='run_bot_console.exe'),
    Executable(script='mass_importer_gui.py', base=base,
               target_name='MassImporter.exe', icon='assets/icon.png'),
]

setup(
    name='Nexus Uploader',
    version='1.0.0',
    description='Nexus Uploader - Manga Upload Tool',
    options={'build_exe': build_options},
    executables=executables,
)
