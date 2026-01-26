"""
GLM API Client (Cerebras) - Fallback для Gemini
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass

import httpx

from .base_client import BaseLLMClient, LLMProvider
from .utils import parse_json_response, JSONParseError
from core.exceptions import LLMResponseError, LLMConnectionError


logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Статистика использования токенов"""
    input_tokens: int = 0
    output_tokens: int = 0
    
    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class GLMClient(BaseLLMClient):
    """Клиент для GLM API (Cerebras)
    
    Основной LLM провайдер.
    Поддерживает Cerebras модели: glm-4.7
    """
    
    API_URL = "https://api.cerebras.ai/v1/chat/completions"
    DEFAULT_MODEL = "zai-glm-4.7"
    DEFAULT_TIMEOUT = 120.0
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0
    
    # Rate limit tracking
    _request_count: int = 0
    _rate_limit_hits: int = 0
    _last_request_time: float = 0
    
    def __init__(self, api_key: str, model_name: Optional[str] = None):
        model = model_name or self.DEFAULT_MODEL
        super().__init__(api_key, model)
        self._client: Optional[httpx.AsyncClient] = None
        # Session token tracking
        self._session_input_tokens: int = 0
        self._session_output_tokens: int = 0
        self._on_usage: Optional[Callable[[int, int], None]] = None
    
    def set_usage_callback(self, callback: Callable[[int, int], None]) -> None:
        """Установить callback для отслеживания токенов"""
        self._on_usage = callback
    
    @property
    def session_usage(self) -> LLMUsage:
        """Получить использование токенов за сессию"""
        return LLMUsage(
            input_tokens=self._session_input_tokens,
            output_tokens=self._session_output_tokens
        )
    
    @property
    def api_stats(self) -> dict:
        """Статистика API вызовов"""
        return {
            "requests": GLMClient._request_count,
            "rate_limit_hits": GLMClient._rate_limit_hits,
            "tokens_in": self._session_input_tokens,
            "tokens_out": self._session_output_tokens,
        }
    
    @property
    def provider(self) -> LLMProvider:
        """Return the provider type"""
        return LLMProvider.GLM
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create reusable HTTP client"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.DEFAULT_TIMEOUT, connect=30.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
            )
        return self._client
    
    async def close(self):
        """Close the HTTP client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        return False
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False,
        max_tokens: int = 2048
    ) -> str:
        """Генерировать ответ с retry логикой
        
        Args:
            prompt: Текст запроса
            temperature: Креативность (0.0-1.0)
            json_mode: Если True, добавляет инструкцию вернуть JSON
            max_tokens: Максимум токенов в ответе (default 2048, use 512 for JSON)
        
        Returns:
            Текст ответа
            
        Raises:
            LLMConnectionError: При ошибке соединения после всех retry
            LLMResponseError: При пустом ответе
        """
        # JSON responses are usually short - limit tokens to save rate limit
        if json_mode:
            prompt = f"{prompt}\n\nRespond with valid JSON only."
            max_tokens = min(max_tokens, 1024)  # JSON rarely needs more
        
        last_error: Optional[Exception] = None
        prompt_preview = prompt[:100].replace('\n', ' ') + '...' if len(prompt) > 100 else prompt.replace('\n', ' ')
        
        # Log full prompt in debug mode
        logger.debug(f"=== GLM PROMPT ===\n{prompt}\n=== END PROMPT ===")
        
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                GLMClient._request_count += 1
                import time
                request_start = time.time()
                GLMClient._last_request_time = request_start
                
                client = await self._get_client()
                response = await client.post(
                    self.API_URL,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": max_tokens
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                # Track token usage
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                elapsed = time.time() - request_start
                
                # Log successful request with full stats
                logger.info(f"GLM #{GLMClient._request_count}: {input_tokens}+{output_tokens} tokens, {elapsed:.1f}s | {prompt_preview}")
                
                choices = data.get("choices", [])
                if not choices:
                    raise LLMResponseError("GLM returned no choices")
                
                message = choices[0].get("message", {})
                content = message.get("content", "")
                reasoning = message.get("reasoning", "")
                
                # GLM thinking models may put response in reasoning field
                if (not content or not content.strip()) and reasoning:
                    logger.debug(f"GLM: content empty, using reasoning field")
                    content = reasoning
                
                # Логируем reasoning если есть и отличается от content
                if reasoning and reasoning != content:
                    logger.debug(f"GLM reasoning: {reasoning[:200]}...")
                    
                self._session_input_tokens += input_tokens
                self._session_output_tokens += output_tokens
                
                # Callback if set
                if self._on_usage:
                    self._on_usage(input_tokens, output_tokens)
                
                if not content or not content.strip():
                    # Log full response for debugging
                    logger.warning(f"GLM empty response, full data: {data}")
                    raise LLMResponseError("GLM returned empty response")
                
                # Log full response in debug mode
                logger.debug(f"=== GLM RESPONSE ===\n{content}\n=== END RESPONSE ===")
                
                return content
                
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"GLM timeout (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"GLM HTTP error {e.response.status_code} (attempt {attempt}/{self.MAX_RETRIES})")
                if e.response.status_code == 429:
                    await asyncio.sleep(self.RETRY_DELAY * attempt * 2)
                    continue
            except Exception as e:
                last_error = e
                logger.warning(f"GLM error (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            
            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * (2 ** (attempt - 1)))
        
        raise LLMConnectionError(f"GLM failed after {self.MAX_RETRIES} attempts: {last_error}")
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ"""
        response = await self.generate(prompt, temperature, json_mode=True)
        try:
            return parse_json_response(response)
        except JSONParseError:
            raise
        except Exception as e:
            raise JSONParseError(f"Failed to parse JSON from GLM response: {e}", raw_text=response)
    
    async def generate_with_reasoning(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048
    ) -> tuple[str, str]:
        """Генерировать ответ с reasoning (для thinking моделей)
        
        Returns:
            Tuple[content, reasoning]
        """
        last_error: Optional[Exception] = None
        
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                client = await self._get_client()
                response = await client.post(
                    self.API_URL,
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": max_tokens
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                choices = data.get("choices", [])
                if not choices:
                    raise LLMResponseError("GLM returned no choices")
                
                message = choices[0].get("message", {})
                content = message.get("content", "")
                reasoning = message.get("reasoning", "")
                
                # Track token usage
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._session_input_tokens += input_tokens
                self._session_output_tokens += output_tokens
                
                if self._on_usage:
                    self._on_usage(input_tokens, output_tokens)
                
                if not content or not content.strip():
                    raise LLMResponseError("GLM returned empty response")
                
                return content, reasoning
                
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"GLM timeout (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"GLM HTTP error {e.response.status_code} (attempt {attempt}/{self.MAX_RETRIES})")
                if e.response.status_code == 429:
                    await asyncio.sleep(self.RETRY_DELAY * attempt * 2)
                    continue
            except Exception as e:
                last_error = e
                logger.warning(f"GLM error (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            
            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * (2 ** (attempt - 1)))
        
        raise LLMConnectionError(f"GLM failed after {self.MAX_RETRIES} attempts: {last_error}")
