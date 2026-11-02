"""Notifikasi hasil ke Telegram (pesan teks dan voice note)."""

import os

import requests


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID belum diset di .env")
        return cls(token, chat_id)

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
            if response.status_code == 200:
                return True
            if parse_mode:
                # Fallback: Markdown tidak valid -> kirim teks polos.
                payload.pop("parse_mode")
                response = requests.post(
                    f"{self._base}/sendMessage", json=payload, timeout=10
                )
                return response.status_code == 200
            return False
        except requests.RequestException as exc:
            print(f"⚠️ Gagal kirim Telegram: {exc}")
            return False

    def send_voice(self, audio_path: str, caption: str = "") -> bool:
        try:
            with open(audio_path, "rb") as audio:
                response = requests.post(
                    f"{self._base}/sendVoice",
                    data={"chat_id": self.chat_id, "caption": caption},
                    files={"voice": audio},
                    timeout=30,
                )
            return response.status_code == 200
        except (OSError, requests.RequestException) as exc:
            print(f"⚠️ Gagal kirim voice Telegram: {exc}")
            return False
