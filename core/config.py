"""
Configuration System - Pydantic Settings с .env поддержкой
"""

import shutil
from pathlib import Path
from typing import Optional, Literal, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, model_validator


class Config(BaseSettings):
    """Конфигурация Bender"""
    
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )
    
    # Обязательно - GLM (Cerebras) - единственный LLM
    glm_api_key: str
    droid_project_path: str
    
    # Алиас для glm_api_key
    cerebras_api_key: Optional[str] = None
    
    # Опционально - Droid
    droid_binary: str = "droid"
    
    # Опционально - Git
    auto_git_push: bool = True
    
    # Опционально - Display
    display_mode: Literal["visible", "silent"] = "visible"
    
    # Опционально - Bender behavior
    bender_escalate_after: int = 5
    
    # Опционально - Watchdog
    watchdog_interval: int = 300
    watchdog_timeout: int = 3600
    
    # Опционально - Droid Controller
    idle_timeout: int = 120
    check_interval: float = 2.0
    
    # Опционально - LLM settings (только GLM)
    llm_max_retries: int = 3
    llm_retry_delay: float = 1.0
    llm_requests_per_minute: int = 60
    
    # Опционально - Analyzer settings
    analyzer_truncate_length: int = 3000
    analyzer_truncate_start_ratio: float = 0.4
    
    # Опционально - Conversation history
    max_conversation_history: int = 100
    
    # Директории
    log_dir: str = "logs"
    state_dir: str = "state"
    
    @field_validator('droid_project_path')
    @classmethod
    def validate_project_path(cls, v: str) -> str:
        """Validate that project path exists"""
        path = Path(v).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Project path does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Project path is not a directory: {path}")
        return str(path)
    
    @field_validator('droid_binary')
    @classmethod
    def validate_droid_binary(cls, v: str) -> str:
        """Validate that droid binary exists in PATH or is absolute path"""
        if Path(v).is_absolute():
            if not Path(v).exists():
                raise ValueError(f"Droid binary not found: {v}")
            return v
        if shutil.which(v) is None:
            raise ValueError(f"Droid binary '{v}' not found in PATH")
        return v
    
    @field_validator('bender_escalate_after')
    @classmethod
    def validate_escalate_after(cls, v: int) -> int:
        """Validate escalate_after is positive"""
        if v < 1:
            raise ValueError("bender_escalate_after must be at least 1")
        return v
    
    @field_validator('analyzer_truncate_start_ratio')
    @classmethod
    def validate_truncate_ratio(cls, v: float) -> float:
        """Validate truncate ratio is between 0 and 1"""
        if not 0 <= v <= 1:
            raise ValueError("analyzer_truncate_start_ratio must be between 0 and 1")
        return v
    
    @model_validator(mode='after')
    def use_cerebras_as_glm_fallback(self) -> 'Config':
        """Use cerebras_api_key as fallback for glm_api_key"""
        if self.glm_api_key is None and self.cerebras_api_key is not None:
            try:
                self.glm_api_key = self.cerebras_api_key
            except Exception:
                object.__setattr__(self, 'glm_api_key', self.cerebras_api_key)
        return self
    
    @property
    def project_path(self) -> Path:
        return Path(self.droid_project_path)
    
    @property
    def logs_path(self) -> Path:
        return Path(self.log_dir)
    
    @property
    def state_path(self) -> Path:
        return Path(self.state_dir)
    
    def get_validation_errors(self) -> List[str]:
        """Get list of validation warnings (non-fatal issues)"""
        warnings = []
        
        # Check if tmux is available
        if shutil.which('tmux') is None:
            warnings.append("tmux is not installed or not in PATH")
        
        # Check disk space
        try:
            import os
            stat = os.statvfs(self.project_path)
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb < 100:
                warnings.append(f"Low disk space: {free_mb:.1f}MB free")
        except Exception:
            pass
        
        # Check if git repo
        git_dir = self.project_path / '.git'
        if not git_dir.exists():
            warnings.append("Project is not a git repository")
        
        return warnings


def load_config(env_file: Optional[str] = None) -> Config:
    """Загрузить конфигурацию"""
    if env_file:
        return Config(_env_file=env_file)
    return Config()
