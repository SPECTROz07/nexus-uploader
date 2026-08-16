# -*- coding: utf-8 -*-

import json
from pathlib import Path
from cryptography.fernet import Fernet

from core.master_key import obter_chave

class CredentialsManager:
    def __init__(self):
        self.config_dir = Path.home() / "Documents" / "SpectralModeração"
        self.config_file = self.config_dir / "login.json"
        self._ensure_config_dir()
        # chave-mestra centralizada e protegida por DPAPI (ver core/master_key.py)
        self.key = obter_chave()
        self.fernet = Fernet(self.key)

    def _ensure_config_dir(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _encrypt(self, data: str) -> str:
        return self.fernet.encrypt(data.encode('utf-8')).decode('utf-8')

    def _decrypt(self, token: str) -> str:
        if not token: return ""
        try:
            return self.fernet.decrypt(token.encode('utf-8')).decode('utf-8')
        except Exception:
            return ""

    def save_credentials(self, username, password):
        credentials = {'username': self._encrypt(username), 'password': self._encrypt(password)}
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(credentials, f)

    def load_credentials(self):
        if not self.config_file.exists():
            return None, None
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                credentials = json.load(f)
            username = self._decrypt(credentials.get('username', ''))
            password = self._decrypt(credentials.get('password', ''))
            return (username, password) if username and password else (None, None)
        except Exception:
            return None, None