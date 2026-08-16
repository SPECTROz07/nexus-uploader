# --- START OF FILE core/s3_handler.py ---

import boto3
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

def _s3_fernets():
    """(chave portátil do vault, [fallbacks]) — o cofre R2 abre em qualquer
    máquina; o fallback DPAPI migra um cofre antigo cifrado por-máquina."""
    from core import vault
    from core.master_key import fernet as _mk_fernet
    return vault.fernet(), [_mk_fernet()]


def carregar_s3_config(config_path='s3_config.json') -> dict:
    from core.secure_store import carregar_json_seguro
    f, fb = _s3_fernets()
    return carregar_json_seguro(config_path, fernet=f, fernets_fallback=fb)


def salvar_s3_config(dados: dict, config_path='s3_config.json'):
    """Grava as chaves R2 no cofre portátil (cifrado). Não deixa .json em claro."""
    from core.secure_store import salvar_json_seguro
    f, _ = _s3_fernets()
    return salvar_json_seguro(config_path, dados, fernet=f)


def s3_config_existe(config_path='s3_config.json') -> bool:
    from core.secure_store import esta_cifrado
    return esta_cifrado(config_path) or Path(config_path).exists()


# O nome da classe foi mantido para compatibilidade com o resto do seu aplicativo
class ContaboS3Handler:
    def __init__(self, config_path='s3_config.json'):
        from core.secure_store import esta_cifrado
        try:
            config = carregar_s3_config(config_path)
        except RuntimeError as e:
            raise Exception(str(e))
        if not config:
            if esta_cifrado(config_path):
                raise Exception(f"Configuração '{config_path}' cifrada porém vazia/ilegível.")
            raise Exception(f"Arquivo de configuração '{config_path}' não encontrado.")

        # Chaves necessárias para R2 e outros S3-compatíveis
        required_keys = ['access_key', 'secret_key', 'endpoint_url', 'bucket_name', 'cdn_base_url']
        if not all(key in config for key in required_keys):
            raise Exception(f"O arquivo de configuração '{config_path}' precisa ter as chaves: {', '.join(required_keys)}.")

        self.access_key = config['access_key']
        self.secret_key = config['secret_key']
        self.endpoint_url = config['endpoint_url']
        self.bucket_name = config['bucket_name']
        self.path_prefix = config.get('path_prefix', "").strip('/')
        self.cdn_base_url = config['cdn_base_url'].strip('/')

        # Configuração robusta do Boto3 com timeouts e retentativas (mantida)
        s3_botocore_config = BotocoreConfig(
            s3={'addressing_style': 'path'},
            connect_timeout=15,
            read_timeout=60,
            retries={
                'max_attempts': 3,
                'mode': 'standard'
            }
        )
        
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            # Assume que o endpoint_url no config já está completo (ex: https://...)
            endpoint_url=self.endpoint_url,
            config=s3_botocore_config
        )

    def test_connection(self):
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            return True, "Sucesso! Conexão com S3/R2 e acesso ao bucket validados."
        except ClientError as e:
            return False, f"Erro ao acessar o bucket '{self.bucket_name}': {e}"

    def upload_file_obj(self, file_obj, object_key, content_type='image/webp'):
        max_retries = 3
        base_delay = 1

        for attempt in range(max_retries):
            try:
                full_s3_key = f"{self.path_prefix}/{object_key}".lstrip('/') if self.path_prefix else object_key
                file_obj.seek(0)

                # ### CORREÇÃO PARA R2 ###
                # O parâmetro ACL='public-read' foi REMOVIDO pois não é suportado pelo R2.
                self.s3_client.put_object(
                    Body=file_obj, 
                    Bucket=self.bucket_name, 
                    Key=full_s3_key,
                    ContentType=content_type
                )

                # ### CORREÇÃO PARA R2 ###
                # A URL da CDN é construída de forma genérica, sem o 'bucket_id'.
                cdn_url = f"https://{self.cdn_base_url}/{quote(full_s3_key)}"
                
                # A função retorna o caminho relativo (`object_key`) como você precisa.
                return True, {"cdn_url": cdn_url, "object_key": full_s3_key}

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                if error_code in ['SlowDown', 'TooManyRequests', 'RequestTimeout', 'InternalError']:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Erro recuperável no S3/R2 ({error_code}) para o objeto {object_key}. "
                            f"Tentativa {attempt + 1}/{max_retries}. "
                            f"Aguardando {delay:.2f}s para tentar novamente."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"Todas as {max_retries} tentativas de upload falharam para {object_key} devido a '{error_code}'.")
                        return False, f"Erro S3/R2: {e.response['Error']['Message']}"
                else:
                    return False, f"Erro S3/R2 (não recuperável): {e.response['Error']['Message']}"
            except Exception as e:
                return False, str(e)
        
        return False, "Falha no upload para o S3/R2 após todas as tentativas."

    def list_chapter_files(self, chapter_prefix):
        full_prefix_path = f"{self.path_prefix}/{chapter_prefix}".lstrip('/') if self.path_prefix else chapter_prefix
        
        all_files = []
        paginator = self.s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=full_prefix_path)
        
        try:
            for page in pages:
                if 'Contents' in page:
                    all_files.extend(page['Contents'])
            return all_files
        except ClientError as e:
            logger.error(f"Erro ao listar arquivos no S3/R2 para o prefixo {full_prefix_path}: {e}")
            return None

    def delete_files(self, object_keys):
        if not object_keys: return True, "Nenhum arquivo para deletar."
        
        deleted_count_total = 0
        error_messages_total = []
        
        full_keys = [f"{self.path_prefix}/{key}".lstrip('/') if self.path_prefix else key for key in object_keys]

        # A API delete_objects aceita até 1000 chaves por vez
        for i in range(0, len(full_keys), 1000):
            chunk = full_keys[i:i + 1000]
            objects_to_delete = [{'Key': key} for key in chunk]
            
            try:
                response = self.s3_client.delete_objects(Bucket=self.bucket_name, Delete={'Objects': objects_to_delete})
                deleted_count_total += len(response.get('Deleted', []))
                
                if 'Errors' in response and response['Errors']:
                    errors = response.get('Errors', [])
                    error_messages = [f"Key: {e['Key']}, Code: {e['Code']}, Message: {e['Message']}" for e in errors]
                    error_messages_total.extend(error_messages)
            except ClientError as e:
                return False, f"Erro de API ao deletar um lote de arquivos do S3/R2: {e}"

        if error_messages_total:
            return False, f"Falha ao deletar {len(error_messages_total)} arquivo(s). Detalhes: {'; '.join(error_messages_total)}"
        
        return True, f"{deleted_count_total} arquivo(s) deletado(s) com sucesso do S3/R2."

    def delete_folder(self, folder_prefix):
        files_in_folder = self.list_chapter_files(folder_prefix)
        if files_in_folder is None: return False, f"Erro ao tentar listar arquivos na pasta '{folder_prefix}'."
        if not files_in_folder: return True, "Nenhum arquivo encontrado na pasta para deletar."
        
        # O S3 armazena o caminho completo, então usamos isso para deletar
        keys_to_delete = [obj['Key'] for obj in files_in_folder]
        
        # A função delete_files já lida com o path_prefix, então não precisamos removê-lo aqui
        return self.delete_files(keys_to_delete)