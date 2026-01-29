"""
Structured Logging Configuration

Supports both human-readable and JSON formats for log analysis.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add extra fields if present
        if hasattr(record, "step_id"):
            log_data["step_id"] = record.step_id
        if hasattr(record, "iteration"):
            log_data["iteration"] = record.iteration
        if hasattr(record, "action"):
            log_data["action"] = record.action
        if hasattr(record, "provider"):
            log_data["provider"] = record.provider
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data, ensure_ascii=False)


class ColoredFormatter(logging.Formatter):
    """Colored formatter for terminal output"""
    
    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO",
    log_dir: Optional[str] = None,
    json_format: bool = False,
    log_file: Optional[str] = None,
    file_level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR"]] = None,
    quiet: bool = True  # По умолчанию тихий режим - меньше мусора
) -> logging.Logger:
    """Setup logging configuration
    
    Args:
        level: Console log level
        log_dir: Directory for log files
        json_format: Use JSON format for file logs
        log_file: Specific log file name (auto-generated if None)
        file_level: File log level (defaults to level if not specified)
        quiet: Suppress noisy library logs (httpx, etc)
    
    Returns:
        Root logger
    """
    root_logger = logging.getLogger()
    # Root level = minimum of console and file levels
    file_lvl = file_level or level
    min_level = min(getattr(logging, level), getattr(logging, file_lvl))
    root_logger.setLevel(min_level)
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler - только важные сообщения
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level))
    if sys.stdout.isatty():
        console_formatter = ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        )
    else:
        console_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # Suppress noisy library logs
    if quiet:
        # Эти библиотеки очень шумные на INFO уровне
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        # Детали воркеров - только WARNING и выше в консоль
        logging.getLogger("bender.workers.base").setLevel(logging.WARNING)
        logging.getLogger("bender.worker_manager").setLevel(logging.WARNING)
        logging.getLogger("bender.glm_client").setLevel(logging.WARNING)
    
    # File handler
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        
        if log_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = f"parser_maker_{timestamp}.log"
        
        file_handler = logging.FileHandler(
            log_path / log_file,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        
        if json_format:
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
        
        root_logger.addHandler(file_handler)
    
    return root_logger


class LogContext:
    """Context manager for adding extra fields to log records"""
    
    def __init__(self, logger: logging.Logger, **kwargs):
        self.logger = logger
        self.extra = kwargs
        self._old_factory = None
    
    def __enter__(self):
        self._old_factory = logging.getLogRecordFactory()
        extra = self.extra
        
        def record_factory(*args, **kwargs):
            record = self._old_factory(*args, **kwargs)
            for key, value in extra.items():
                setattr(record, key, value)
            return record
        
        logging.setLogRecordFactory(record_factory)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.setLogRecordFactory(self._old_factory)
        return False
