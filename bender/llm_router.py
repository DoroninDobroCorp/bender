"""
LLM Router - GLM primary, Qwen fallback with key rotation
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable, TypeVar, List

from .glm_client import GLMClient


logger = logging.getLogger(__name__)

T = TypeVar('T')

# Модели - используем только Qwen (стабильный, без thinking)
PRIMARY_MODEL = "qwen-3-235b-a22b-instruct-2507"
FALLBACK_MODEL = "qwen-3-235b-a22b-instruct-2507"  # тот же, на случай если код ожидает fallback


class RateLimiter:
    """Adaptive rate limiter - увеличивает delay при 429"""
    
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.min_delay = 5.0  # Базовый delay 5 секунд
        self.current_delay = self.min_delay
        self.max_delay = 120.0  # Максимум 2 минуты между запросами
        self.last_request = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """Wait until a request can be made"""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_request
            
            if elapsed < self.current_delay:
                wait_time = self.current_delay - elapsed
                logger.debug(f"Rate limit: waiting {wait_time:.1f}s between requests")
                await asyncio.sleep(wait_time)
            
            self.last_request = time.time()
    
    def on_success(self):
        """Успешный запрос - уменьшаем delay"""
        self.current_delay = max(self.min_delay, self.current_delay * 0.8)
    
    def on_rate_limit(self):
        """429 - увеличиваем delay"""
        self.current_delay = min(self.max_delay, self.current_delay * 2)
        logger.warning(f"Rate limit hit, increasing delay to {self.current_delay:.1f}s")


class KeyRotator:
    """Rotates between multiple API keys to avoid rate limits"""
    
    def __init__(self, keys: List[str]):
        self.keys = keys if keys else []
        self.current_index = 0
        self.failed_keys: Dict[str, float] = {}  # key -> failure time
        self.cooldown = 60.0  # Cerebras нужно 60s cooldown после 429
        self._lock = asyncio.Lock()
    
    async def get_key(self) -> str:
        """Get next available API key"""
        async with self._lock:
            if not self.keys:
                raise ValueError("No API keys configured")
            
            now = time.time()
            # Try to find a working key
            for _ in range(len(self.keys)):
                key = self.keys[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.keys)
                
                # Check if key is in cooldown
                if key in self.failed_keys:
                    if now - self.failed_keys[key] < self.cooldown:
                        continue  # Skip this key
                    else:
                        del self.failed_keys[key]  # Cooldown expired
                
                return key
            
            # All keys failed - wait for shortest cooldown to expire
            if self.failed_keys:
                oldest_fail = min(self.failed_keys.values())
                wait_time = max(0, self.cooldown - (now - oldest_fail)) + 1
                logger.info(f"All API keys in cooldown, waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
                # Clear expired cooldowns
                self.failed_keys = {k: v for k, v in self.failed_keys.items() 
                                   if now + wait_time - v < self.cooldown}
            
            return self.keys[0]
    
    async def mark_failed(self, key: str):
        """Mark a key as failed (rate limited)"""
        async with self._lock:
            self.failed_keys[key] = time.time()
            logger.warning(f"API key ...{key[-8:]} marked as rate-limited (cooldown {self.cooldown}s)")


class LLMRouter:
    """Роутер с GLM primary и Qwen fallback + key rotation
    
    Primary: zai-glm-4.7 (thinking model)
    Fallback: qwen-3-235b-a22b-instruct-2507
    
    При 429 автоматически переключается на следующий ключ.
    """
    
    def __init__(
        self,
        glm_api_key: str,
        gemini_api_key: Optional[str] = None,  # игнорируется
        glm_model: str = PRIMARY_MODEL,
        requests_per_minute: int = 60,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        api_keys: Optional[List[str]] = None,  # Multiple keys for rotation
        **kwargs  # игнорируем остальные параметры
    ):
        self.api_key = glm_api_key
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Key rotation - use provided list or single key
        self.all_keys = api_keys if api_keys else [glm_api_key]
        self.key_rotator = KeyRotator(self.all_keys)
        logger.info(f"LLMRouter initialized with {len(self.all_keys)} API key(s)")
        
        # Rate limiter
        self.rate_limiter = RateLimiter(requests_per_minute)
        
        # Clients будут создаваться динамически с разными ключами
        self._glm_clients: Dict[str, GLMClient] = {}
        self._qwen_clients: Dict[str, GLMClient] = {}
        
        # Статистика
        self.stats: Dict[str, int] = {
            "glm_calls": 0,
            "glm_errors": 0,
            "qwen_calls": 0,
            "qwen_errors": 0,
            "fallbacks": 0,
            "key_rotations": 0,
        }
        
        self._last_provider: str = "glm"
        self._usage_callback: Optional[Callable[[int, int], None]] = None
    
    def _get_glm_client(self, api_key: str) -> GLMClient:
        """Get or create GLM client for specific key"""
        if api_key not in self._glm_clients:
            self._glm_clients[api_key] = GLMClient(api_key, PRIMARY_MODEL)
            if self._usage_callback:
                self._glm_clients[api_key].set_usage_callback(self._usage_callback)
        return self._glm_clients[api_key]
    
    def _get_qwen_client(self, api_key: str) -> GLMClient:
        """Get or create Qwen client for specific key"""
        if api_key not in self._qwen_clients:
            self._qwen_clients[api_key] = GLMClient(api_key, FALLBACK_MODEL)
            if self._usage_callback:
                self._qwen_clients[api_key].set_usage_callback(self._usage_callback)
        return self._qwen_clients[api_key]
    
    def set_usage_callback(self, callback: Callable[[int, int], None]) -> None:
        """Установить callback для отслеживания токенов"""
        self._usage_callback = callback
        # Применить к уже созданным клиентам
        for client in self._glm_clients.values():
            client.set_usage_callback(callback)
        for client in self._qwen_clients.values():
            client.set_usage_callback(callback)
    
    @property
    def last_provider(self) -> str:
        return self._last_provider
    
    async def close(self):
        """Close all clients"""
        for client in self._glm_clients.values():
            await client.close()
        for client in self._qwen_clients.values():
            await client.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
    
    async def _try_with_key(
        self,
        api_key: str,
        model_type: str,  # "glm" or "qwen"
        prompt: str,
        temperature: float,
        json_mode: bool,
        max_tokens: int = 4096
    ) -> Optional[str]:
        """Try to generate with specific key and model
        
        Returns:
            Response string if success
            None if transient error (can retry with another key)
        
        Raises:
            RuntimeError if x-should-retry=false (don't retry with other keys)
        """
        if model_type == "glm":
            client = self._get_glm_client(api_key)
        else:
            client = self._get_qwen_client(api_key)
        
        try:
            await self.rate_limiter.acquire()
            response = await client.generate(prompt, temperature, json_mode, max_tokens=max_tokens)
            self.stats[f"{model_type}_calls"] += 1
            self._last_provider = model_type
            self.rate_limiter.on_success()  # Успех - можно уменьшить delay
            return response
        except Exception as e:
            error_str = str(e)
            self.stats[f"{model_type}_errors"] += 1
            
            # При 429 помечаем ключ как failed и увеличиваем delay
            if "429" in error_str or "rate limit" in error_str.lower():
                self.rate_limiter.on_rate_limit()  # Увеличить delay
                await self.key_rotator.mark_failed(api_key)
                self.stats["key_rotations"] += 1
                
                # Если x-should-retry=false - не пробовать другие ключи
                if "retry disabled" in error_str.lower():
                    logger.warning(f"GLM global rate limit - waiting {self.key_rotator.cooldown}s")
                    raise RuntimeError(f"GLM rate limit, wait required")
            
            logger.warning(f"{model_type.upper()} error with key ...{api_key[-8:]}: {e}")
            return None
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False,
        max_tokens: int = 4096
    ) -> str:
        """Генерировать ответ: перебирает ключи с паузами при 429"""
        
        # Пробуем каждый ключ
        for attempt in range(len(self.all_keys)):
            api_key = await self.key_rotator.get_key()
            logger.debug(f"Using key ...{api_key[-8:]} (attempt {attempt + 1}/{len(self.all_keys)})")
            
            # Пауза перед повторной попыткой (после первой неудачи)
            if attempt > 0:
                wait_time = 10 + attempt * 5  # 10, 15, 20 секунд
                logger.info(f"🔄 Retry {attempt + 1}/{len(self.all_keys)} with key ...{api_key[-8:]}, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            
            try:
                response = await self._try_with_key(api_key, "glm", prompt, temperature, json_mode, max_tokens)
                if response:
                    logger.debug(f"✅ Key ...{api_key[-8:]} succeeded")
                    return response
            except RuntimeError as e:
                if "wait required" in str(e):
                    # x-should-retry=false: этот ключ в rate limit, переходим к следующему
                    logger.warning(f"⏳ Key ...{api_key[-8:]} rate limited, trying next key")
                    continue  # Следующий ключ!
                else:
                    raise
        
        # Все ключи не сработали - ждём и пробуем ещё раз
        logger.warning(f"All {len(self.all_keys)} keys failed, waiting 60s before final retry")
        await asyncio.sleep(60)
        
        # Последняя попытка с первым ключом
        api_key = self.all_keys[0]
        response = await self._try_with_key(api_key, "glm", prompt, temperature, json_mode, max_tokens)
        if response:
            return response
        
        raise RuntimeError(f"All API keys failed (tried {len(self.all_keys)} keys)")
    
    async def generate_simple(
        self,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 50
    ) -> str:
        """Простой запрос без thinking (использует Qwen напрямую)
        
        Для простых да/нет вопросов где не нужен мыслительный процесс.
        """
        for attempt in range(len(self.all_keys)):
            api_key = await self.key_rotator.get_key()
            response = await self._try_with_key(api_key, "qwen", prompt, temperature, False, max_tokens)
            if response:
                return response
        
        raise RuntimeError(f"Simple generate failed with all {len(self.all_keys)} API keys")
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ с fallback"""
        from .utils import parse_json_response, JSONParseError
        
        response = await self.generate(prompt, temperature, json_mode=True, max_tokens=max_tokens)
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
        last_error = None
        for attempt in range(len(self.all_keys)):
            api_key = await self.key_rotator.get_key()
            client = self._get_glm_client(api_key)
            try:
                await self.rate_limiter.acquire()
                content, reasoning = await client.generate_with_reasoning(prompt, temperature)
                self.stats["glm_calls"] += 1
                self._last_provider = "glm"
                return content, reasoning
            except Exception as e:
                last_error = e
                self.stats["glm_errors"] += 1
                logger.warning(f"GLM reasoning error with key ...{api_key[-8:]}: {e}")
                if "429" in str(e).lower():
                    await self.key_rotator.mark_failed(api_key)
                if attempt < len(self.all_keys) - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
        
        # Fallback to Qwen (no separate reasoning, but has <think> tags)
        logger.warning(f"⚠️  GLM failed, falling back to QWEN for reasoning")
        self.stats["fallbacks"] += 1
        
        response = None
        for attempt in range(len(self.all_keys)):
            api_key = await self.key_rotator.get_key()
            response = await self._try_with_key(api_key, "qwen", prompt, temperature, False)
            if response:
                break
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
        
        raise RuntimeError(f"All LLM providers failed for reasoning (GLM + Qwen): {last_error}")
    
    def get_stats(self) -> Dict[str, int]:
        """Получить статистику"""
        return self.stats.copy()
