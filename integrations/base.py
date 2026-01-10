"""
Base Notifier - abstract interface for notifications
"""

from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass
from enum import Enum


class NotificationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


@dataclass
class Notification:
    """Notification message"""
    title: str
    message: str
    level: NotificationLevel = NotificationLevel.INFO
    step_id: Optional[int] = None
    iteration: Optional[int] = None
    extra: Optional[dict] = None


class BaseNotifier(ABC):
    """Abstract base class for notifiers"""
    
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send notification
        
        Args:
            notification: Notification to send
            
        Returns:
            True if sent successfully
        """
        pass
    
    @abstractmethod
    async def send_escalation(self, reason: str, context: dict) -> bool:
        """Send escalation notification (high priority)
        
        Args:
            reason: Escalation reason
            context: Additional context (step, iteration, etc.)
            
        Returns:
            True if sent successfully
        """
        pass
    
    async def send_step_complete(self, step_id: int, step_name: str, iterations: int) -> bool:
        """Send step completion notification"""
        return await self.send(Notification(
            title=f"Step {step_id} Complete",
            message=f"{step_name} completed in {iterations} iterations",
            level=NotificationLevel.SUCCESS,
            step_id=step_id
        ))
    
    async def send_pipeline_complete(self, total_iterations: int, total_commits: int) -> bool:
        """Send pipeline completion notification"""
        return await self.send(Notification(
            title="Pipeline Complete",
            message=f"Finished with {total_iterations} iterations and {total_commits} commits",
            level=NotificationLevel.SUCCESS
        ))
    
    async def send_error(self, error: str, step_id: Optional[int] = None) -> bool:
        """Send error notification"""
        return await self.send(Notification(
            title="Error",
            message=error,
            level=NotificationLevel.ERROR,
            step_id=step_id
        ))
