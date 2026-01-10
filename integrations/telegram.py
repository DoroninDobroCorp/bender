"""
Telegram Notifier - send notifications via Telegram Bot API
"""

import logging
from typing import Optional

import httpx

from .base import BaseNotifier, Notification, NotificationLevel


logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    """Telegram notification sender
    
    Requires:
        - TELEGRAM_BOT_TOKEN: Bot token from @BotFather
        - TELEGRAM_CHAT_ID: Chat/channel ID to send messages to
    """
    
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    LEVEL_EMOJI = {
        NotificationLevel.INFO: "ℹ️",
        NotificationLevel.WARNING: "⚠️",
        NotificationLevel.ERROR: "❌",
        NotificationLevel.SUCCESS: "✅",
    }
    
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bot_token and chat_id
        
        if not self.enabled:
            logger.info("Telegram notifications disabled (missing token or chat_id)")
    
    async def send(self, notification: Notification) -> bool:
        """Send notification to Telegram"""
        if not self.enabled:
            return False
        
        emoji = self.LEVEL_EMOJI.get(notification.level, "")
        
        # Format message
        text = f"{emoji} *{notification.title}*\n\n{notification.message}"
        
        if notification.step_id:
            text += f"\n\n_Step: {notification.step_id}_"
        if notification.iteration:
            text += f" | _Iteration: {notification.iteration}_"
        
        return await self._send_message(text)
    
    async def send_escalation(self, reason: str, context: dict) -> bool:
        """Send escalation notification"""
        if not self.enabled:
            return False
        
        text = f"🚨 *ESCALATION REQUIRED*\n\n{reason}"
        
        if context:
            text += "\n\n*Context:*"
            for key, value in context.items():
                text += f"\n• {key}: {value}"
        
        return await self._send_message(text)
    
    async def _send_message(self, text: str) -> bool:
        """Send message via Telegram API"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.API_URL.format(token=self.bot_token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown"
                    }
                )
                
                if response.status_code == 200:
                    return True
                
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False
