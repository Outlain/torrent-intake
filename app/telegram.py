from __future__ import annotations
import httpx
from .config import get_settings


class TelegramService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def send_infected_deleted(self, *, torrent_name: str | None, qbt_hash: str | None, staging_path: str | None,
                              final_parent: str, threat_name: str | None) -> None:
        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if not token or not chat_id:
            return

        text = (
            "🚨 Torrent intake malware deletion\n"
            f"Torrent: {torrent_name or 'unknown'}\n"
            f"Hash: {qbt_hash or 'unknown'}\n"
            f"Threat: {threat_name or 'unknown'}\n"
            f"Staging path: {staging_path or 'unknown'}\n"
            f"Final parent: {final_parent}"
        )

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with httpx.Client(timeout=10) as client:
            response = client.post(url, json={"chat_id": chat_id, "text": text})
            response.raise_for_status()
