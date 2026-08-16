# --- START OF FILE core/storage_handler.py ---

import pysftp
import json
import logging
import io
import os
import time

logger = logging.getLogger(__name__)

class StorageHandler:
    def __init__(self, config_path='bunny_config.json'):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise Exception(f"Erro ao carregar o arquivo de configuração '{config_path}': {e}")
        
        self.hostname = config['hostname']
        self.username = config['username']
        self.password = config['password']
        
        self.cnopts = pysftp.CnOpts()
        self.cnopts.hostkeys = None

    def test_connection(self):
        try:
            with self.get_connection() as sftp:
                sftp.listdir('/')
            return True, "Sucesso! Conexão com Bunny.net SFTP validada."
        except Exception as e:
            return False, f"Erro ao conectar com Bunny.net SFTP: {e}"

    def get_connection(self):
        """Retorna um novo objeto de conexão SFTP."""
        return pysftp.Connection(
            self.hostname,
            username=self.username,
            password=self.password,
            cnopts=self.cnopts
        )

    def ensure_remote_dir(self, sftp_connection, remote_dir):
        """Garante que um diretório remoto exista, criando-o se necessário, usando uma conexão ativa."""
        if remote_dir and not sftp_connection.lexists(remote_dir):
            logger.info(f"Criando diretório remoto: /{remote_dir}")
            sftp_connection.makedirs(remote_dir)

    def upload_file_obj(self, sftp_connection, file_obj, object_key, content_type=None):
        """
        Faz upload de um objeto de arquivo usando uma conexão SFTP existente.
        Este método agora NÃO gerencia a conexão, apenas a utiliza.
        
        Args:
            sftp_connection: Uma conexão pysftp.Connection ativa.
            file_obj: O objeto de arquivo em bytes (ex: BytesIO).
            object_key: O caminho relativo do arquivo no storage.
        """
        max_retries = 3
        base_delay = 2

        for attempt in range(max_retries):
            try:
                file_obj.seek(0)
                logger.info(f"Fazendo upload para: /{object_key} (Tentativa {attempt + 1})")
                sftp_connection.putfo(file_obj, object_key)
                return True, {"object_key": object_key}
            except Exception as e:
                logger.warning(f"Falha na tentativa {attempt + 1}/{max_retries} de upload SFTP para {object_key}: {e}")
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                else:
                    logger.error(f"Todas as {max_retries} tentativas de upload SFTP falharam para {object_key}.", exc_info=True)
                    return False, str(e)
        
        return False, "Falha no upload para o Bunny.net após todas as tentativas."

    def delete_files(self, object_keys):
        logger.warning("A função 'delete_files' não está implementada para o StorageHandler.")
        return True, "Operação de exclusão ignorada."

    def list_chapter_files(self, chapter_prefix):
        logger.warning("A função 'list_chapter_files' não está implementada para o StorageHandler.")
        return []