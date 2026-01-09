"""
LLM Router - маршрутизация между Gemini и GLM с fallback
"""

import asyncio
import logging
from typing import Optional, Dict, Any, Literal

from .gemini_client import GeminiClient
from .glm_client import GLMClient


logger = logging.getLogger(__name__)


class LLMRouter:
    """Роутер между LLM провайдерами с автоматическим fallback
    
    Primary: Gemini API
    Fallback: GLM (Cerebras)
    
    При недоступности Gemini (5xx, timeout, rate limit) автоматически
    переключается на GLM после 3 попыток.
    """
    
    def __init__(
        self,
        gemini_api_key: str,
        glm_api_key: Optional[str] = None,
        gemini_model: str = "gemini-2.5-pro",
        primary: Literal["gemini", "glm"] = "gemini",
        enable_fallback: bool = True,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        self.primary = primary
        self.enable_fallback = enable_fallback
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Инициализировать клиенты
        self.gemini: Optional[GeminiClient] = None
        self.glm: Optional[GLMClient] = None
        
        if gemini_api_key:
            self.gemini = GeminiClient(gemini_api_key, gemini_model)
        
        if glm_api_key:
            self.glm = GLMClient(glm_api_key)
        
        # Статистика
        self.stats = {
            "gemini_calls": 0,
            "gemini_errors": 0,
            "glm_calls": 0,
            "glm_errors": 0,
            "fallbacks": 0
        }
        
        self._last_provider: Optional[str] = None
    
    @property
    def last_provider(self) -> Optional[str]:
        """Какой провайдер использовался в последнем вызове"""
        return self._last_provider
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Генерировать ответ с автоматическим fallback
        
        Args:
            prompt: Текст запроса
            temperature: Креативность
            json_mode: Режим JSON ответа
        
        Returns:
            Текст ответа
        
        Raises:
            RuntimeError: Если все провайдеры недоступны
        """
        # Определить порядок провайдеров
        if self.primary == "gemini":
            providers = [("gemini", self.gemini), ("glm", self.glm)]
        else:
            providers = [("glm", self.glm), ("gemini", self.gemini)]
        
        if not self.enable_fallback:
            providers = [providers[0]]
        
        last_error = None
        
        for provider_name, client in providers:
            if client is None:
                continue
            
            # Попытки с exponential backoff
            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.debug(f"Trying {provider_name}, attempt {attempt}/{self.max_retries}")
                    
                    response = await client.generate(prompt, temperature, json_mode)
                    
                    # Успех
                    self._last_provider = provider_name
                    self.stats[f"{provider_name}_calls"] += 1
                    
                    if provider_name != self.primary and self.enable_fallback:
                        self.stats["fallbacks"] += 1
                        logger.info(f"Used fallback provider: {provider_name}")
                    
                    return response
                    
                except Exception as e:
                    last_error = e
                    self.stats[f"{provider_name}_errors"] += 1
                    logger.warning(f"{provider_name} error (attempt {attempt}): {e}")
                    
                    if attempt < self.max_retries:
                        delay = self.retry_delay * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
            
            # Все попытки исчерпаны для этого провайдера
            logger.warning(f"{provider_name} failed after {self.max_retries} attempts")
        
        # Все провайдеры недоступны
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ с fallback"""
        # Определить порядок провайдеров
        if self.primary == "gemini":
            providers = [("gemini", self.gemini), ("glm", self.glm)]
        else:
            providers = [("glm", self.glm), ("gemini", self.gemini)]
        
        if not self.enable_fallback:
            providers = [providers[0]]
        
        last_error = None
        
        for provider_name, client in providers:
            if client is None:
                continue
            
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = await client.generate_json(prompt, temperature)
                    
                    self._last_provider = provider_name
                    self.stats[f"{provider_name}_calls"] += 1
                    
                    if provider_name != self.primary and self.enable_fallback:
                        self.stats["fallbacks"] += 1
                    
                    return response
                    
                except Exception as e:
                    last_error = e
                    self.stats[f"{provider_name}_errors"] += 1
                    
                    if attempt < self.max_retries:
                        delay = self.retry_delay * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
        
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")
    
    def get_stats(self) -> Dict[str, int]:
        """Получить статистику использования"""
        return self.stats.copy()
