# core/master_key.py
"""
Guarda da chave-mestra Fernet, protegida pela DPAPI do Windows.

Antes: a chave ficava em claro em Documents/SpectralModeração/key.key — quem
copiasse esse arquivo decifrava tudo. Agora ela é protegida com CryptProtectData
(escopo CURRENT_USER): o blob cifrado só pode ser decifrado pelo MESMO usuário
Windows, na MESMA máquina. Copiar o arquivo pra outra conta/PC não abre nada.

Importante: a CHAVE Fernet em si é preservada — só muda a forma como ela é
guardada no disco. Então login.json, git_update.json e os *.enc já existentes
continuam válidos após a migração (mesma chave, novo cofre de armazenamento).

Fora do Windows (dev/CI): cai no arquivo em claro, como era antes.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet

CONFIG_DIR = Path.home() / "Documents" / "SpectralModeração"
KEY_CLARO = CONFIG_DIR / "key.key"          # legado / fallback não-Windows
KEY_DPAPI = CONFIG_DIR / "key.key.dpapi"    # chave protegida por DPAPI


# --------------------------------------------------------------------------
# DPAPI via ctypes (sem pywin32)
# --------------------------------------------------------------------------
def _dpapi_ok() -> bool:
    return os.name == "nt"


def _dpapi(func_name, entrada: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(entrada, len(entrada))
    blob_in = DATA_BLOB(len(entrada), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    func = getattr(crypt32, func_name)
    # (pDataIn, szDataDescr, pOptionalEntropy, pvReserved, pPromptStruct, dwFlags, pDataOut)
    ok = func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"{func_name} falhou (erro {ctypes.GetLastError()})")
    try:
        saida = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return saida


def _proteger(dados: bytes) -> bytes:
    return _dpapi("CryptProtectData", dados)


def _desproteger(blob: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", blob)


# --------------------------------------------------------------------------
# API pública — os módulos de cifra chamam obter_chave()
# --------------------------------------------------------------------------
def obter_chave() -> bytes:
    """
    Devolve a chave Fernet (bytes). Ordem:
      1. Windows + key.key.dpapi existe -> desprotege e devolve;
      2. key.key em claro existe -> MIGRA (protege em .dpapi, apaga o claro no
         Windows) e devolve a MESMA chave;
      3. nada -> gera nova, guarda protegida (ou em claro fora do Windows).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if _dpapi_ok() and KEY_DPAPI.exists():
        try:
            return _desproteger(KEY_DPAPI.read_bytes())
        except OSError:
            # cofre de outra conta/máquina; não dá pra recuperar por aqui
            raise RuntimeError(
                f"'{KEY_DPAPI.name}' não pôde ser decifrado nesta conta/máquina "
                "(DPAPI é por-usuário). Restaure o backup ou apague para gerar nova.")

    if KEY_CLARO.exists():
        chave = KEY_CLARO.read_bytes()
        if _dpapi_ok():
            KEY_DPAPI.write_bytes(_proteger(chave))
            try:
                KEY_CLARO.unlink()  # remove a versão em claro após migrar
            except OSError:
                pass
        return chave

    # primeira vez: gera nova
    chave = Fernet.generate_key()
    if _dpapi_ok():
        KEY_DPAPI.write_bytes(_proteger(chave))
    else:
        KEY_CLARO.write_bytes(chave)
    return chave


def fernet() -> Fernet:
    return Fernet(obter_chave())
