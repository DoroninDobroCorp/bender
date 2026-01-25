"""
LLM Router - GLM primary, Qwen fallback
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable, TypeVar

from .glm_client import GLMClient


logger = logging.getLogger(__name__)

T = TypeVar('T')

# Модели
PRIMARY_MODEL = "zai-glm-4.7"
FALLBACK_MODEL = "qwen-3-235b-a22b-instruct-2507"


class RateLimiter:
    """Simple token bucket rate limiter"""
    
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.tokens = requests_per_minute
        self.last_update = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """Wait until a request can be made"""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            
            # Refill tokens based on elapsed time
            self.tokens = min(
                self.requests_per_minute,
                self.tokens + elapsed * (self.requests_per_minute / 60)
            )
            self.last_update = now
            
            if self.tokens < 1:
                wait_time = (1 - self.tokens) * (60 / self.requests_per_minute)
                logger.debug(f"Rate limit: waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                self.tokens = 1
            
            self.tokens -= 1


class LLMRouter:
    """Роутер с GLM primary и Qwen fallback
    
    Primary: zai-glm-4.7 (thinking model)
    Fallback: qwen-3-235b-a22b-instruct-2507
    """
    
    def __init__(
        self,
        glm_api_key: str,
        gemini_api_key: Optional[str] = None,  # игнорируется
        glm_model: str = PRIMARY_MODEL,
        requests_per_minute: int = 60,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        **kwargs  # игнорируем остальные параметры
    ):
        self.api_key = glm_api_key
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Rate limiter
        self.rate_limiter = RateLimiter(requests_per_minute)
        
        # Primary: GLM
        self.glm = GLMClient(glm_api_key, PRIMARY_MODEL)
        
        # Fallback: Qwen (тот же API, другая модель)
        self.qwen = GLMClient(glm_api_key, FALLBACK_MODEL)
        
        # Статистика
        self.stats: Dict[str, int] = {
            "glm_calls": 0,
            "glm_errors": 0,
            "qwen_calls": 0,
            "qwen_errors": 0,
            "fallbacks": 0,
        }
        
        self._last_provider: str = "glm"
    
    def set_usage_callback(self, callback: Callable[[int, int], None]) -> None:
        """Установить callback для отслеживания токенов (пробрасывается в GLM клиент)"""
        self.glm.set_usage_callback(callback)
    
    @property
    def last_provider(self) -> str:
        return self._last_provider
    
    async def close(self):
        """Close clients"""
        if self.glm:
            await self.glm.close()
        if self.qwen:
            await self.qwen.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
    
    async def _try_generate(
        self,
        client: GLMClient,
        name: str,
        prompt: str,
        temperature: float,
        json_mode: bool
    ) -> Optional[str]:
        """Try to generate with a specific client"""
        try:
            await self.rate_limiter.acquire()
            response = await client.generate(prompt, temperature, json_mode)
            self.stats[f"{name}_calls"] += 1
            self._last_provider = name
            return response
        except Exception as e:
            self.stats[f"{name}_errors"] += 1
            logger.warning(f"{name.upper()} error: {e}")
            return None
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Генерировать ответ: GLM -> Qwen fallback"""
        
        # Try GLM first
        response = await self._try_generate(self.glm, "glm", prompt, temperature, json_mode)
        if response:
            return response
        
        # Fallback to Qwen
        logger.warning(f"⚠️  GLM failed, falling back to QWEN ({FALLBACK_MODEL})")
        self.stats["fallbacks"] += 1
        
        response = await self._try_generate(self.qwen, "qwen", prompt, temperature, json_mode)
        if response:
            logger.info(f"✅ QWEN fallback succeeded")
            return response
        
        raise RuntimeError(f"All LLM providers failed (GLM + Qwen fallback)")
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ с fallback"""
        from .utils import parse_json_response, JSONParseError
        
        response = await self.generate(prompt, temperature, json_mode=True)
        try:
            return parse_json_response(response)
        except JSONParseError:
            raise
    
    async def generate_with_reasoning(
        self,
        prompt: str,
        temperature: float = 0.7,
    ) -> tuple[str, str]:
        """Генерировать ответ с reasoning (GLM thinking)
        
        Returns:
            Tuple[content, reasoning]
        """
        # Try GLM first (has reasoning)
        for attempt in range(1, self.max_retries + 1):
            try:
                await self.rate_limiter.acquire()
                content, reasoning = await self.glm.generate_with_reasoning(prompt, temperature)
                self.stats["glm_calls"] += 1
                self._last_provider = "glm"
                return content, reasoning
            except Exception as e:
                self.stats["glm_errors"] += 1
                logger.warning(f"GLM reasoning error (attempt {attempt}/{self.max_retries}): {e}")
                
                if attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
        
        # Fallback to Qwen (no separate reasoning, but has <think> tags)
        logger.warning(f"⚠️  GLM failed, falling back to QWEN for reasoning")
        self.stats["fallbacks"] += 1
        
        response = await self._try_generate(self.qwen, "qwen", prompt, temperature, False)
        if response:
            # Qwen puts thinking in <think> tags
            import re
            think_match = re.search(r'<think>(.*?)</think>', response, re.DOTALL)
            if think_match:
                reasoning = think_match.group(1).strip()
                content = re.sub(r'<think>.*?</think>\s*', '', response, flags=re.DOTALL).strip()
            else:
                reasoning = ""
                content = response
            
            logger.info(f"✅ QWEN fallback succeeded")
            return content, reasoning
        
        raise RuntimeError(f"All LLM providers failed for reasoning (GLM + Qwen)")
    
    def get_stats(self) -> Dict[str, int]:
        """Получить статистику"""
        return self.stats.copy()
