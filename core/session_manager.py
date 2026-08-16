# core/session_manager.py
import json
from pathlib import Path
import threading

from dataclasses import dataclass, asdict, field

@dataclass
class SessionData:
    domain: str
    headers: dict
    cookies: dict

class SessionManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(SessionManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, filepath="sessions.json"):
        if self._initialized:
            return
        # ancora na raiz do app (funciona congelado ou do fonte); os cookies de
        # sessão/cf_clearance ficam CIFRADOS em sessions.json.enc (secure_store)
        p = Path(filepath)
        if not p.is_absolute():
            from core.paths import raiz_do_app
            p = raiz_do_app() / p
        self.filepath = p
        self.sessions = self._load_sessions()
        self._initialized = True

    def _load_sessions(self):
        from core.secure_store import carregar_json_seguro
        try:
            data = carregar_json_seguro(self.filepath)
        except Exception:
            return {}
        try:
            return {domain: SessionData(**s_data) for domain, s_data in data.items()}
        except (TypeError, AttributeError):
            return {}

    def _save_sessions(self):
        from core.secure_store import salvar_json_seguro
        with self._lock:
            try:
                data_to_save = {domain: asdict(s_data) for domain, s_data in self.sessions.items()}
                salvar_json_seguro(self.filepath, data_to_save)
            except Exception as e:
                print(f"Erro ao salvar sessões: {e}")

    def get_session(self, domain: str) -> SessionData | None:
        return self.sessions.get(domain)

    def insert_session(self, session_data: SessionData):
        self.sessions[session_data.domain] = session_data
        self._save_sessions()

    def delete_session(self, domain: str):
        if domain in self.sessions:
            del self.sessions[domain]
            self._save_sessions()

# Funções globais para serem usadas pelo código do nodriver
_manager = SessionManager()

def get_request(domain: str) -> SessionData | None:
    return _manager.get_session(domain)

def insert_request(session_data: SessionData):
    _manager.insert_session(session_data)

def delete_request(domain: str):
    _manager.delete_session(domain)