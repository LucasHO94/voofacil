"""Canal Telegram — POST direto na Bot API (sem dependência extra).

Bot criado no @BotFather; chat_id obtido enviando /start ao bot e lendo
https://api.telegram.org/bot<TOKEN>/getUpdates
"""
from __future__ import annotations

import json
import logging

import httpx

from farewatch.alerts.mensagem import Mensagem
from farewatch.config import settings

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"


class TelegramChannel:
    nome = "TELEGRAM"

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 client: httpx.Client | None = None):
        self.token = token or settings.telegram_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self._client = client or httpx.Client(base_url=API_URL, timeout=15.0)

    @property
    def configurado(self) -> bool:
        return bool(self.token and self.chat_id)

    def enviar(self, msg: Mensagem) -> bool:
        if not self.configurado:
            log.warning("Telegram não configurado (TELEGRAM_TOKEN/CHAT_ID)")
            return False
        payload: dict = {
            "chat_id": self.chat_id,
            "text": msg.html,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if msg.deep_link:
            payload["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": "🛒 Ver oferta / comprar", "url": msg.deep_link}]]}
            )
        try:
            resp = self._client.post(f"/bot{self.token}/sendMessage", json=payload)
            resp.raise_for_status()
            return bool(resp.json().get("ok"))
        except httpx.HTTPError as exc:
            log.error("Falha ao enviar Telegram: %s", exc)
            return False
