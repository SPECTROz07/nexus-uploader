# --- START OF FILE core/settings_manager.py ---

import json
from pathlib import Path

class SettingsManager:
    def __init__(self, settings_file="settings.json"):
        self.settings_path = Path(settings_file)
        self.defaults = {
            "s3_config_path": "s3_config.json",
            "works_limit_per_bot": 40,
            "enable_notifications": True,
            "minimize_to_tray": True,
            "nsfw_filters_by_provider": {}
        }
        self.settings = self._load_settings()

    def _load_settings(self):
        if not self.settings_path.exists():
            return self.defaults.copy()
        try:
            with open(self.settings_path, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
            for key, value in self.defaults.items():
                loaded_settings.setdefault(key, value)
            return loaded_settings
        except (json.JSONDecodeError, TypeError):
            return self.defaults.copy()

    def save_settings(self):
        try:
            with open(self.settings_path, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            print(f"Erro ao salvar configurações: {e}")

    def get(self, key, default_value=None):
        return self.settings.get(key, default_value)

    def set(self, key, value):
        self.settings[key] = value
        self.save_settings()