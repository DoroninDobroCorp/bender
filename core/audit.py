"""
Audit Logging Module for Parser Maker

Provides secure audit logging for sensitive operations,
compliance tracking, and security monitoring.
"""

import logging
import json
from typing import Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
import hashlib


logger = logging.getLogger(__name__)


class AuditEventType(Enum):
    """Types of audit events"""
    # Authentication & Authorization
    AUTH_SUCCESS = "auth.success"
    AUTH_FAILURE = "auth.failure"
    
    # API Operations
    API_CALL = "api.call"
    API_ERROR = "api.error"
    
    # Pipeline Operations
    PIPELINE_START = "pipeline.start"
    PIPELINE_STEP = "pipeline.step"
    PIPELINE_COMPLETE = "pipeline.complete"
    PIPELINE_ERROR = "pipeline.error"
    
    # Git Operations
    GIT_COMMIT = "git.commit"
    GIT_PUSH = "git.push"
    GIT_ERROR = "git.error"
    
    # State Operations
    STATE_SAVE = "state.save"
    STATE_LOAD = "state.load"
    STATE_RECOVERY = "state.recovery"
    
    # Configuration
    CONFIG_LOAD = "config.load"
    CONFIG_CHANGE = "config.change"
    
    # Security Events
    SECURITY_WARNING = "security.warning"
    SECURITY_ERROR = "security.error"


class AuditSeverity(Enum):
    """Severity levels for audit events"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class AuditEvent:
    """Audit event record"""
    event_type: AuditEventType
    severity: AuditSeverity
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    actor: Optional[str] = None
    resource: Optional[str] = None
    action: Optional[str] = None
    outcome: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    ip_address: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = {
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
        }
        
        # Add optional fields if present
        if self.actor:
            data["actor"] = self.actor
        if self.resource:
            data["resource"] = self.resource
        if self.action:
            data["action"] = self.action
        if self.outcome:
            data["outcome"] = self.outcome
        if self.details:
            data["details"] = self.details
        if self.request_id:
            data["request_id"] = self.request_id
        if self.session_id:
            data["session_id"] = self.session_id
        if self.ip_address:
            data["ip_address"] = self.ip_address
        
        return data
    
    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict(), default=str)


class AuditLogger:
    """Audit logger for security and compliance
    
    Provides structured audit logging with:
    - JSON formatted logs
    - Log rotation support
    - Tamper detection via checksums
    - Configurable severity filtering
    """
    
    def __init__(
        self,
        log_dir: Optional[str] = None,
        min_severity: AuditSeverity = AuditSeverity.INFO,
        enable_console: bool = False
    ):
        self.log_dir = Path(log_dir) if log_dir else Path("logs/audit")
        self.min_severity = min_severity
        self.enable_console = enable_console
        self._events: list = []
        self._checksum_chain: str = ""
        
        # Ensure log directory exists
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup file handler
        self._setup_logger()
    
    def _setup_logger(self):
        """Setup audit logger"""
        self._audit_logger = logging.getLogger("audit")
        self._audit_logger.setLevel(logging.DEBUG)
        
        # File handler with JSON formatting
        log_file = self.log_dir / f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl"
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        self._audit_logger.addHandler(file_handler)
        
        if self.enable_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(
                logging.Formatter("[AUDIT] %(message)s")
            )
            self._audit_logger.addHandler(console_handler)
    
    def _should_log(self, severity: AuditSeverity) -> bool:
        """Check if event should be logged based on severity"""
        severity_order = [
            AuditSeverity.DEBUG,
            AuditSeverity.INFO,
            AuditSeverity.WARNING,
            AuditSeverity.ERROR,
            AuditSeverity.CRITICAL
        ]
        return severity_order.index(severity) >= severity_order.index(self.min_severity)
    
    def _compute_checksum(self, event_json: str) -> str:
        """Compute checksum for tamper detection"""
        data = f"{self._checksum_chain}{event_json}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def log(self, event: AuditEvent) -> None:
        """Log an audit event"""
        if not self._should_log(event.severity):
            return
        
        # Add checksum for tamper detection
        event_dict = event.to_dict()
        event_json = json.dumps(event_dict, default=str)
        checksum = self._compute_checksum(event_json)
        event_dict["_checksum"] = checksum
        self._checksum_chain = checksum
        
        # Log to file
        self._audit_logger.info(json.dumps(event_dict, default=str))
        
        # Store in memory (limited)
        self._events.append(event)
        if len(self._events) > 1000:
            self._events = self._events[-500:]
    
    def log_api_call(
        self,
        provider: str,
        model: str,
        success: bool,
        latency_ms: Optional[float] = None,
        tokens: Optional[int] = None,
        error: Optional[str] = None
    ):
        """Log an API call"""
        self.log(AuditEvent(
            event_type=AuditEventType.API_CALL if success else AuditEventType.API_ERROR,
            severity=AuditSeverity.INFO if success else AuditSeverity.WARNING,
            message=f"API call to {provider}/{model}",
            action="api_call",
            outcome="success" if success else "failure",
            details={
                "provider": provider,
                "model": model,
                "latency_ms": latency_ms,
                "tokens": tokens,
                "error": error
            }
        ))
    
    def log_pipeline_event(
        self,
        event_type: AuditEventType,
        step: Optional[int] = None,
        message: str = "",
        details: Optional[Dict[str, Any]] = None
    ):
        """Log a pipeline event"""
        self.log(AuditEvent(
            event_type=event_type,
            severity=AuditSeverity.INFO,
            message=message,
            resource=f"step_{step}" if step else None,
            details=details or {}
        ))
    
    def log_security_event(
        self,
        message: str,
        severity: AuditSeverity = AuditSeverity.WARNING,
        details: Optional[Dict[str, Any]] = None
    ):
        """Log a security event"""
        event_type = (
            AuditEventType.SECURITY_ERROR 
            if severity in [AuditSeverity.ERROR, AuditSeverity.CRITICAL]
            else AuditEventType.SECURITY_WARNING
        )
        self.log(AuditEvent(
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {}
        ))
    
    def get_recent_events(
        self,
        count: int = 100,
        event_type: Optional[AuditEventType] = None
    ) -> list:
        """Get recent audit events"""
        events = self._events
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-count:]


# Global audit logger instance
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """Get global audit logger instance"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger


def audit_log(event: AuditEvent) -> None:
    """Log an audit event"""
    get_audit_logger().log(event)


def audit_api_call(**kwargs) -> None:
    """Log an API call"""
    get_audit_logger().log_api_call(**kwargs)


def audit_security(message: str, **kwargs) -> None:
    """Log a security event"""
    get_audit_logger().log_security_event(message, **kwargs)
