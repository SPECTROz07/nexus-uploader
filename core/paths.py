# core/paths.py
"""
Raiz do aplicativo, correta nos DOIS modos de rodar:

- do código-fonte (clone + venv): a pasta do projeto;
- congelado (cx_Freeze): a pasta do executável — onde o include_files
  deposita providers/, assets/ etc. como arquivos EXTERNOS de propósito,
  pra sincronização de providers pelo repo funcionar sem rebuild.

Nunca calcular caminho de providers/assets com __file__ direto: dentro do
exe congelado o __file__ pode apontar pra dentro do lib/ e quebrar tudo.
"""
import sys
from pathlib import Path


def raiz_do_app() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def pasta_providers() -> Path:
    return raiz_do_app() / "providers"


def pasta_assets() -> Path:
    return raiz_do_app() / "assets"
