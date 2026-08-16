# -*- coding: utf-8 -*-
"""
Troca as chaves do Cloudflare R2 no cofre cifrado (s3_config.json.enc), sem
mexer no endpoint/bucket. Use depois de gerar um novo par no painel da Cloudflare
(R2 -> Manage R2 API Tokens -> Create) e revogar o antigo.

Uso:
    venv\\Scripts\\python.exe rotacionar_r2.py --access-key NOVA --secret-key NOVA
    (sem argumentos, ele pergunta interativamente)

O resultado já sai cifrado; nenhuma chave é escrita em claro em disco.
"""
import argparse
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from core.s3_handler import carregar_s3_config, salvar_s3_config

ALVO = "s3_config.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--access-key")
    ap.add_argument("--secret-key")
    args = ap.parse_args()

    atual = carregar_s3_config(ALVO)
    if not atual:
        print("Nenhum s3_config encontrado. Crie o s3_config.json a partir do "
              "s3_config.example.json e rode o app uma vez (ele cifra sozinho).")
        sys.exit(1)

    print(f"Endpoint: {atual.get('endpoint_url', '?')}  |  bucket: {atual.get('bucket_name', '?')}")
    print(f"access_key atual: {str(atual.get('access_key',''))[:6]}… (será substituída)")

    nova_ak = args.access_key or input("Nova access_key: ").strip()
    nova_sk = args.secret_key or input("Nova secret_key: ").strip()
    if not nova_ak or not nova_sk:
        print("Chaves vazias — abortado.")
        sys.exit(1)

    atual["access_key"] = nova_ak
    atual["secret_key"] = nova_sk
    salvar_s3_config(atual, ALVO)
    print("Cofre atualizado (s3_config.json.enc).")

    # valida a conexão com as novas chaves antes de dar por certo
    try:
        from core.s3_handler import ContaboS3Handler
        ok, msg = ContaboS3Handler(config_path=ALVO).test_connection()
        print(("OK — R2 conectou com as novas chaves." if ok
               else f"ATENÇÃO — as novas chaves NÃO conectaram: {msg}"))
    except Exception as e:
        print(f"Não deu pra validar agora: {e}")


if __name__ == "__main__":
    main()
