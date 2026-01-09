"""
Configuration System - Pydantic Settings с .env поддержкой
"""

from pathlib import Path
from typing import Optional, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Конфигурация Parser Maker"""
    
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )
    
    # Обязательно
    gemini_api_key: str
    droid_project_path: str
    
    # Опционально - Bender
    glm_api_key: Optional[str] = None  # fallback при недоступности Gemini
    
    # Опционально - Droid
    droid_binary: str = "droid"
    
    # Опционально - Git
    auto_git_push: bool = True
    
    # Опционально - Display
    display_mode: Literal["visible", "silent"] = "visible"
    
    # Опционально - Bender behavior
    bender_escalate_after: int = 5  # после скольких неудач спрашивать человека
    
    # Опционально - Watchdog
    watchdog_interval: int = 300  # секунд между проверками (5 мин)
    watchdog_timeout: int = 3600  # общий таймаут на задачу (1 час)
    
    # Опционально - Droid Controller
    idle_timeout: int = 120  # секунд ожидания ответа
    check_interval: float = 2.0  # секунд между проверками output
    
    # Директории
    log_dir: str = "logs"
    state_dir: str = "state"
    
    @property
    def project_path(self) -> Path:
        return Path(self.droid_project_path)
    
    @property
    def logs_path(self) -> Path:
        return Path(self.log_dir)
    
    @property
    def state_path(self) -> Path:
        return Path(self.state_dir)


def load_config(env_file: Optional[str] = None) -> Config:
    """Загрузить конфигурацию"""
    if env_file:
        return Config(_env_file=env_file)
    return Config()
