# core/secure_store.py
"""
Cofre para os arquivos de configuração sensíveis do uploader.

Duas chaves possíveis, escolhidas por quem chama:
  - master_key (DPAPI, presa ao usuário/máquina) para dados PESSOAIS que NÃO
    devem viajar (login, sessões);
  - vault (portátil, embutida em .pyd) para as chaves do R2, que VÃO no pacote
    e precisam abrir na máquina de qualquer uploader — mas cifradas pra que ele
    não leia/copie, só troque.

Um arquivo sensível vira `<nome>.enc` (token Fernet); o `.json` em claro é
MIGRADO na primeira leitura e apagado. `fernets_fallback` permite reabrir um
cofre cifrado com uma chave antiga e regravá-lo com a nova (migração DPAPI→vault).

Modelo de ameaça honesto: protege contra leitura casual (disco, backup, GUI).
NÃO protege contra quem inspeciona a memória do app rodando nem contra um
programador — pra usar a chave, o app precisa decifrá-la em runtime.
"""
import json
from pathlib import Path

from cryptography.fernet import InvalidToken

from core.master_key import fernet as _mk_fernet, KEY_DPAPI as KEY_FILE


def _fernet_de(fernet):
    return fernet if fernet is not None else _mk_fernet()


def _caminho_enc(caminho_claro) -> Path:
    p = Path(caminho_claro)
    return p.with_name(p.name + ".enc")


def esta_cifrado(caminho_claro) -> bool:
    return _caminho_enc(caminho_claro).exists()


def salvar_json_seguro(caminho_claro, dados: dict, fernet=None) -> Path:
    """Grava `dados` cifrado em <caminho>.enc e remove o .json em claro, se houver."""
    enc = _caminho_enc(caminho_claro)
    enc.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dados, ensure_ascii=False).encode("utf-8")
    enc.write_bytes(_fernet_de(fernet).encrypt(payload))
    claro = Path(caminho_claro)
    if claro.exists():
        try:
            claro.unlink()
        except OSError:
            pass
    return enc


def carregar_json_seguro(caminho_claro, migrar=True, fernet=None, fernets_fallback=None) -> dict:
    """
    Devolve o dict do config sensível. Ordem:
      1. <caminho>.enc existe -> decifra (com `fernet`; se falhar, tenta cada
         `fernets_fallback` e, se abrir, RE-GRAVA com `fernet`) e devolve;
      2. <caminho> (claro) existe -> lê, MIGRA pra .enc (apagando o claro), devolve;
      3. nada -> {}.
    """
    f_primario = _fernet_de(fernet)
    enc = _caminho_enc(caminho_claro)
    if enc.exists():
        bruto = enc.read_bytes()
        try:
            return json.loads(f_primario.decrypt(bruto).decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError):
            pass
        for f_alt in (fernets_fallback or []):
            try:
                dados = json.loads(f_alt.decrypt(bruto).decode("utf-8"))
                # reabriu com chave antiga -> regrava com a nova (migração)
                enc.write_bytes(f_primario.encrypt(json.dumps(dados, ensure_ascii=False).encode("utf-8")))
                return dados
            except (InvalidToken, json.JSONDecodeError):
                continue
        raise RuntimeError(
            f"Não foi possível decifrar '{enc.name}' com nenhuma chave conhecida.")

    claro = Path(caminho_claro)
    if claro.exists():
        with open(claro, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if migrar:
            salvar_json_seguro(caminho_claro, dados, fernet=fernet)
        return dados

    return {}
