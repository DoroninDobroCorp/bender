"""
LLM Router - маршрутизация между Gemini и GLM с fallback, rate limiting и circuit breaker
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Literal, Callable, TypeVar, List, Tuple, Union
from enum import Enum

from .gemini_client import GeminiClient
from .glm_client import GLMClient


logger = logging.getLogger(__name__)

T = TypeVar('T')
ProviderType = Literal["gemini", "glm"]


class CircuitState(str, Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """Circuit breaker for LLM providers
    
    Prevents cascading failures by temporarily disabling failing providers.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()
    
    @property
    def state(self) -> CircuitState:
        return self._state
    
    async def can_execute(self) -> bool:
        """Check if request can be executed"""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("Circuit breaker: OPEN -> HALF_OPEN")
                    return True
                return False
            
            if self._state == CircuitState.HALF_OPEN:
                # Allow limited calls in half-open state
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            
            return False
    
    async def record_success(self):
        """Record successful call"""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info("Circuit breaker: HALF_OPEN -> CLOSED (recovered)")
            self._failure_count = 0
    
    async def record_failure(self):
        """Record failed call"""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker: HALF_OPEN -> OPEN (still failing)")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(f"Circuit breaker: CLOSED -> OPEN (failures: {self._failure_count})")
    
    def reset(self):
        """Reset circuit breaker state"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._half_open_calls = 0


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
                # Wait for token to be available
                wait_time = (1 - self.tokens) * (60 / self.requests_per_minute)
                logger.debug(f"Rate limit: waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                self.tokens = 1
            
            self.tokens -= 1


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
        primary: ProviderType = "gemini",
        enable_fallback: bool = True,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        requests_per_minute: int = 60,
        circuit_failure_threshold: int = 5,
        circuit_recovery_timeout: float = 60.0
    ):
        self.primary: ProviderType = primary
        self.enable_fallback = enable_fallback
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Rate limiter
        self.rate_limiter = RateLimiter(requests_per_minute)
        
        # Circuit breakers per provider
        self._circuit_breakers: Dict[ProviderType, CircuitBreaker] = {
            "gemini": CircuitBreaker(circuit_failure_threshold, circuit_recovery_timeout),
            "glm": CircuitBreaker(circuit_failure_threshold, circuit_recovery_timeout)
        }
        
        # Инициализировать клиенты
        self.gemini: Optional[GeminiClient] = None
        self.glm: Optional[GLMClient] = None
        
        if gemini_api_key:
            self.gemini = GeminiClient(gemini_api_key, gemini_model)
        
        if glm_api_key:
            self.glm = GLMClient(glm_api_key)
        
        # Статистика
        self.stats: Dict[str, int] = {
            "gemini_calls": 0,
            "gemini_errors": 0,
            "glm_calls": 0,
            "glm_errors": 0,
            "fallbacks": 0,
            "circuit_breaks": 0
        }
        
        self._last_provider: Optional[ProviderType] = None
    
    @property
    def last_provider(self) -> Optional[ProviderType]:
        """Какой провайдер использовался в последнем вызове"""
        return self._last_provider
    
    def _get_providers(self) -> List[Tuple[ProviderType, Optional[Union[GeminiClient, GLMClient]]]]:
        """Get ordered list of providers based on primary setting"""
        if self.primary == "gemini":
            providers: List[Tuple[ProviderType, Optional[Union[GeminiClient, GLMClient]]]] = [
                ("gemini", self.gemini), ("glm", self.glm)
            ]
        else:
            providers = [("glm", self.glm), ("gemini", self.gemini)]
        
        if not self.enable_fallback:
            providers = [providers[0]]
        
        return providers
    
    async def close(self):
        """Close all LLM clients and release resources"""
        if self.gemini:
            await self.gemini.close()
        if self.glm:
            await self.glm.close()
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        return False
    
    async def health_check(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Check health of all configured LLM providers
        
        Returns:
            Dict with provider status: {"gemini": {"ok": bool, "error": str|None}, ...}
        """
        results: Dict[str, Any] = {}
        
        async def check_provider(name: ProviderType, client: Optional[Union[GeminiClient, GLMClient]]) -> Tuple[ProviderType, bool, Optional[str]]:
            if client is None:
                return name, False, "Not configured"
            try:
                response = await asyncio.wait_for(
                    client.generate("Say 'ok'", temperature=0),
                    timeout=timeout
                )
                return name, bool(response), None
            except asyncio.TimeoutError:
                return name, False, f"Timeout ({timeout}s)"
            except Exception as e:
                return name, False, str(e)
        
        tasks = []
        if self.gemini:
            tasks.append(check_provider("gemini", self.gemini))
        if self.glm:
            tasks.append(check_provider("glm", self.glm))
        
        if tasks:
            check_results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in check_results:
                if isinstance(result, Exception):
                    continue
                name, ok, error = result
                results[name] = {"ok": ok, "error": error}
        
        return results
    
    async def _call_with_retry(
        self,
        call_fn: Callable,
        method_name: str = "generate"
    ) -> T:
        """Generic retry logic for LLM calls with circuit breaker
        
        Args:
            call_fn: Async function that takes (client) and returns response
            method_name: Name for logging purposes
        
        Returns:
            Response from successful call
        
        Raises:
            RuntimeError: If all providers failed
        """
        providers = self._get_providers()
        last_error = None
        
        for provider_name, client in providers:
            if client is None:
                continue
            
            # Check circuit breaker
            circuit = self._circuit_breakers[provider_name]
            if not await circuit.can_execute():
                logger.debug(f"Circuit breaker OPEN for {provider_name}, skipping")
                self.stats["circuit_breaks"] += 1
                continue
            
            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.debug(f"Trying {provider_name} {method_name}, attempt {attempt}/{self.max_retries}")
                    
                    # Apply rate limiting
                    await self.rate_limiter.acquire()
                    
                    response = await call_fn(client)
                    
                    # Success - record and return
                    await circuit.record_success()
                    self._last_provider = provider_name
                    self.stats[f"{provider_name}_calls"] += 1
                    
                    if provider_name != self.primary and self.enable_fallback:
                        self.stats["fallbacks"] += 1
                        logger.info(f"Used fallback provider: {provider_name}")
                    
                    return response
                    
                except Exception as e:
                    last_error = e
                    self.stats[f"{provider_name}_errors"] += 1
                    logger.warning(f"{provider_name} {method_name} error (attempt {attempt}): {e}")
                    
                    if attempt < self.max_retries:
                        delay = self.retry_delay * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
            
            # All retries failed for this provider
            await circuit.record_failure()
            logger.warning(f"{provider_name} failed after {self.max_retries} attempts")
        
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")
    
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
        return await self._call_with_retry(
            lambda client: client.generate(prompt, temperature, json_mode),
            method_name="generate"
        )
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ с fallback"""
        return await self._call_with_retry(
            lambda client: client.generate_json(prompt, temperature),
            method_name="generate_json"
        )
    
    def get_stats(self) -> Dict[str, int]:
        """Получить статистику использования"""
        return self.stats.copy()
