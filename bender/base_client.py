"""
Abstract base class for LLM clients.

Provides a common interface for all LLM providers (Gemini, GLM, etc.)
following the Strategy pattern for easy provider switching.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Protocol, runtime_checkable
from dataclasses import dataclass
from enum import Enum


class LLMProvider(Enum):
    """Supported LLM providers"""
    GEMINI = "gemini"
    GLM = "glm"


@dataclass
class LLMResponse:
    """Standardized LLM response"""
    content: str
    provider: LLMProvider
    model: str
    tokens_used: Optional[int] = None
    latency_ms: Optional[float] = None


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM clients - defines the interface all clients must implement"""
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Generate text response"""
        ...
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Generate JSON response"""
        ...
    
    async def close(self) -> None:
        """Close client and release resources"""
        ...


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients
    
    Provides common functionality and enforces interface contract.
    All LLM clients should inherit from this class.
    """
    
    def __init__(self, api_key: str, model_name: str):
        self.api_key = api_key
        self.model_name = model_name
        self._is_closed = False
    
    @property
    @abstractmethod
    def provider(self) -> LLMProvider:
        """Return the provider type"""
        ...
    
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Generate text response
        
        Args:
            prompt: Input prompt
            temperature: Creativity level (0.0-1.0)
            json_mode: If True, instruct model to return JSON
            
        Returns:
            Generated text
            
        Raises:
            LLMConnectionError: Connection failed
            LLMResponseError: Invalid response
        """
        ...
    
    @abstractmethod
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Generate JSON response
        
        Args:
            prompt: Input prompt
            temperature: Creativity level (0.0-1.0)
            
        Returns:
            Parsed JSON as dict
            
        Raises:
            JSONParseError: Failed to parse response as JSON
        """
        ...
    
    @abstractmethod
    async def close(self) -> None:
        """Close client and release resources"""
        ...
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        return False
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(provider={self.provider.value}, model={self.model_name})"
