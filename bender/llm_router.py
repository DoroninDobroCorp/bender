"""
LLM Router - Cerebras primary, Gemini fallback

Архитектура:
1. Primary: Cerebras (qwen-3-235b-a22b-instruct-2507) - быстрый, но rate limits
2. Fallback: Gemini (gemini-2.0-flash-exp) - стабильный, бесплатный

При 429 от Cerebras автоматически переключается на Gemini.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable, List

from .glm_client import GLMClient
from .gemini_client import GeminiClient, GeminiKeyRotator

logger = logging.getLogger(__name__)

# Модели
CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"
GEMINI_MODEL = "gemini-3-flash-preview"  # Gemini 3 Flash для fallback


class KeyRotator:
    """Ротация между API ключами"""
    
    def __init__(self, keys: List[str], cooldown: float = 60.0):
        self.keys = keys if keys else []
        self.current_index = 0
        self.failed_keys: Dict[str, float] = {}
        self.cooldown = cooldown
        self._lock = asyncio.Lock()
    
    async def get_key(self) -> Optional[str]:
        """Get next available API key, or None if all in cooldown"""
        async with self._lock:
            if not self.keys:
                return None
            
            now = time.time()
            for _ in range(len(self.keys)):
                key = self.keys[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.keys)
                
                if key in self.failed_keys:
                    if now - self.failed_keys[key] < self.cooldown:
                        continue
                    else:
                        del self.failed_keys[key]
                
                return key
            
            return None  # Все ключи в cooldown
    
    async def mark_failed(self, key: str):
        async with self._lock:
            self.failed_keys[key] = time.time()
            logger.warning(f"Key ...{key[-8:]} rate-limited for {self.cooldown}s")
    
    def has_available_keys(self) -> bool:
        """Check if any key is available"""
        now = time.time()
        for key in self.keys:
            if key not in self.failed_keys:
                return True
            if now - self.failed_keys[key] >= self.cooldown:
                return True
        return False


class LLMRouter:
    """Роутер LLM с Gemini fallback
    
    Primary: Cerebras (быстрый)
    Fallback: Gemini (стабильный)
    
    Логика:
    1. Пробуем Cerebras
    2. Если все ключи в rate limit -> Gemini
    3. Если Gemini тоже failed -> ошибка
    """
    
    def __init__(
        self,
        glm_api_key: Optional[str] = None,
        api_keys: Optional[List[str]] = None,
        gemini_api_keys: Optional[List[str]] = None,
        requests_per_minute: int = 60,
        **kwargs
    ):
        # Cerebras ключи
        cerebras_keys = api_keys if api_keys else ([glm_api_key] if glm_api_key else [])
        self.cerebras_rotator = KeyRotator(cerebras_keys, cooldown=60.0)
        self._cerebras_clients: Dict[str, GLMClient] = {}
        
        # Gemini ключи
        self.gemini_keys = gemini_api_keys if gemini_api_keys else []
        self.gemini_rotator = KeyRotator(self.gemini_keys, cooldown=30.0)
        self._gemini_clients: Dict[str, GeminiClient] = {}
        
        # Rate limiting
        self.min_delay = 3.0
        self.last_request = 0.0
        self._lock = asyncio.Lock()
        
        # Stats
        self.stats = {
            "cerebras_calls": 0,
            "cerebras_errors": 0,
            "gemini_calls": 0,
            "gemini_errors": 0,
            "fallbacks": 0,
        }
        
        self._last_provider = "none"
        
        # Logging
        logger.info(
            f"LLMRouter: {len(cerebras_keys)} Cerebras keys, "
            f"{len(self.gemini_keys)} Gemini keys"
        )
    
    def _get_cerebras_client(self, api_key: str) -> GLMClient:
        if api_key not in self._cerebras_clients:
            self._cerebras_clients[api_key] = GLMClient(api_key, CEREBRAS_MODEL)
        return self._cerebras_clients[api_key]
    
    def _get_gemini_client(self, api_key: str) -> GeminiClient:
        if api_key not in self._gemini_clients:
            self._gemini_clients[api_key] = GeminiClient(api_key, GEMINI_MODEL)
        return self._gemini_clients[api_key]
    
    async def _rate_limit(self):
        """Simple rate limiting"""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_request
            if elapsed < self.min_delay:
                await asyncio.sleep(self.min_delay - elapsed)
            self.last_request = time.time()
    
    async def close(self):
        for client in self._cerebras_clients.values():
            await client.close()
        for client in self._gemini_clients.values():
            await client.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self.close()
    
    @property
    def last_provider(self) -> str:
        return self._last_provider
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False,
        max_tokens: int = 4096
    ) -> str:
        """Генерировать ответ: Cerebras -> Gemini fallback"""
        
        # 1. Пробуем Cerebras
        cerebras_key = await self.cerebras_rotator.get_key()
        if cerebras_key:
            try:
                await self._rate_limit()
                client = self._get_cerebras_client(cerebras_key)
                response = await client.generate(prompt, temperature, json_mode, max_tokens)
                self.stats["cerebras_calls"] += 1
                self._last_provider = "cerebras"
                logger.debug(f"✅ Cerebras succeeded")
                return response
            except Exception as e:
                self.stats["cerebras_errors"] += 1
                error_str = str(e)
                
                if "429" in error_str or "rate limit" in error_str.lower():
                    await self.cerebras_rotator.mark_failed(cerebras_key)
                    logger.warning(f"Cerebras rate limit, trying fallback")
                else:
                    logger.warning(f"Cerebras error: {e}")
        
        # 2. Fallback на Gemini
        if self.gemini_keys:
            gemini_key = await self.gemini_rotator.get_key()
            if gemini_key:
                try:
                    self.stats["fallbacks"] += 1
                    client = self._get_gemini_client(gemini_key)
                    response = await client.generate(prompt, temperature, max_tokens)
                    self.stats["gemini_calls"] += 1
                    self._last_provider = "gemini"
                    logger.info(f"✅ Gemini fallback succeeded")
                    return response
                except Exception as e:
                    self.stats["gemini_errors"] += 1
                    if "429" in str(e):
                        await self.gemini_rotator.mark_failed(gemini_key)
                    logger.warning(f"Gemini error: {e}")
        
        # 3. Все провайдеры failed - ждём и retry
        logger.warning("All LLM providers failed, waiting 30s...")
        await asyncio.sleep(30)
        
        # Retry Cerebras
        cerebras_key = await self.cerebras_rotator.get_key()
        if cerebras_key:
            client = self._get_cerebras_client(cerebras_key)
            response = await client.generate(prompt, temperature, json_mode, max_tokens)
            return response
        
        raise RuntimeError("All LLM providers failed")
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """Генерировать JSON"""
        from .utils import parse_json_response
        
        response = await self.generate(prompt, temperature, json_mode=True, max_tokens=max_tokens)
        return parse_json_response(response)
    
    async def generate_simple(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 100
    ) -> str:
        """Простой быстрый запрос (Gemini preferred для скорости)"""
        
        # Для простых запросов предпочитаем Gemini (быстрее)
        if self.gemini_keys:
            gemini_key = await self.gemini_rotator.get_key()
            if gemini_key:
                try:
                    client = self._get_gemini_client(gemini_key)
                    return await client.generate(prompt, temperature, max_tokens)
                except Exception as e:
                    logger.debug(f"Gemini simple failed: {e}")
        
        # Fallback на Cerebras
        return await self.generate(prompt, temperature, max_tokens=max_tokens)
    
    async def generate_with_reasoning(
        self,
        prompt: str,
        temperature: float = 0.7,
    ) -> tuple:
        """Генерировать с reasoning (для совместимости)"""
        response = await self.generate(prompt, temperature)
        return response, ""  # Reasoning пустой, Gemini не поддерживает
    
    def get_stats(self) -> Dict[str, int]:
        return self.stats.copy()
    
    def set_usage_callback(self, callback: Callable[[int, int], None]) -> None:
        """Для совместимости"""
        pass
