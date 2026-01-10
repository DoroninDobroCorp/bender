"""
Slack Notifier - send notifications via Slack Webhook
"""

import logging
from typing import Optional

import httpx

from .base import BaseNotifier, Notification, NotificationLevel


logger = logging.getLogger(__name__)


class SlackNotifier(BaseNotifier):
    """Slack notification sender via Incoming Webhook
    
    Requires:
        - SLACK_WEBHOOK_URL: Incoming webhook URL from Slack app
    """
    
    LEVEL_EMOJI = {
        NotificationLevel.INFO: ":information_source:",
        NotificationLevel.WARNING: ":warning:",
        NotificationLevel.ERROR: ":x:",
        NotificationLevel.SUCCESS: ":white_check_mark:",
    }
    
    LEVEL_COLOR = {
        NotificationLevel.INFO: "#36a64f",
        NotificationLevel.WARNING: "#ff9800",
        NotificationLevel.ERROR: "#dc3545",
        NotificationLevel.SUCCESS: "#28a745",
    }
    
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        channel: Optional[str] = None,
        enabled: bool = True
    ):
        self.webhook_url = webhook_url
        self.channel = channel
        self.enabled = enabled and webhook_url
        
        if not self.enabled:
            logger.info("Slack notifications disabled (missing webhook_url)")
    
    async def send(self, notification: Notification) -> bool:
        """Send notification to Slack"""
        if not self.enabled:
            return False
        
        emoji = self.LEVEL_EMOJI.get(notification.level, "")
        color = self.LEVEL_COLOR.get(notification.level, "#36a64f")
        
        # Build attachment
        attachment = {
            "color": color,
            "title": f"{emoji} {notification.title}",
            "text": notification.message,
            "fields": []
        }
        
        if notification.step_id:
            attachment["fields"].append({
                "title": "Step",
                "value": str(notification.step_id),
                "short": True
            })
        
        if notification.iteration:
            attachment["fields"].append({
                "title": "Iteration",
                "value": str(notification.iteration),
                "short": True
            })
        
        payload = {"attachments": [attachment]}
        
        if self.channel:
            payload["channel"] = self.channel
        
        return await self._send_webhook(payload)
    
    async def send_escalation(self, reason: str, context: dict) -> bool:
        """Send escalation notification"""
        if not self.enabled:
            return False
        
        fields = [{"title": k, "value": str(v), "short": True} for k, v in context.items()]
        
        payload = {
            "attachments": [{
                "color": "#dc3545",
                "title": ":rotating_light: ESCALATION REQUIRED",
                "text": reason,
                "fields": fields
            }]
        }
        
        if self.channel:
            payload["channel"] = self.channel
        
        return await self._send_webhook(payload)
    
    async def _send_webhook(self, payload: dict) -> bool:
        """Send payload to Slack webhook"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.webhook_url,
                    json=payload
                )
                
                if response.status_code == 200:
                    return True
                
                logger.error(f"Slack webhook error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return False
