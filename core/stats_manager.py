# core/stats_manager.py
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime, timedelta

class StatsManager:
    def __init__(self, stats_file="stats.json"):
        self.stats_path = Path(stats_file)
        self.stats = self._load_stats()

    def _load_stats(self):
        if not self.stats_path.exists():
            return {"uploads_by_day": {}}
        try:
            with open(self.stats_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, TypeError):
            return {"uploads_by_day": {}}

    def _save_stats(self):
        try:
            with open(self.stats_path, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=4)
        except Exception as e:
            print(f"Erro ao salvar estatísticas: {e}")

    def record_upload_success(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        uploads_today = self.stats["uploads_by_day"].get(today_str, 0)
        self.stats["uploads_by_day"][today_str] = uploads_today + 1
        self._cleanup_old_stats()
        self._save_stats()

    def _cleanup_old_stats(self):
        """Remove entradas mais antigas que 30 dias para manter o arquivo pequeno."""
        thirty_days_ago = datetime.now() - timedelta(days=30)
        self.stats["uploads_by_day"] = {
            date_str: count
            for date_str, count in self.stats["uploads_by_day"].items()
            if datetime.strptime(date_str, "%Y-%m-%d") >= thirty_days_ago
        }

    def get_stats_summary(self):
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        
        uploads_today = self.stats["uploads_by_day"].get(today_str, 0)
        
        uploads_this_week = 0
        for i in range(7):
            day = today - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            uploads_this_week += self.stats["uploads_by_day"].get(day_str, 0)
            
        total_uploads = sum(self.stats["uploads_by_day"].values())
        
        return {
            "today": uploads_today,
            "week": uploads_this_week,
            "total": total_uploads
        }
        
    def get_last_7_days_data(self):
        data = {}
        today = datetime.now()
        for i in range(7):
            day = today - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            day_label = day.strftime("%d/%m")
            data[day_label] = self.stats["uploads_by_day"].get(day_str, 0)
        return dict(reversed(list(data.items())))