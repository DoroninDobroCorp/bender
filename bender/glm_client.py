"""
GLM API Client (Cerebras) - Fallback для Gemini
"""

import asyncio
import logging
from typing import Dict, Any, Optional

import httpx

from .base_client import BaseLLMClient, LLMProvider
from .utils import parse_json_response, JSONParseError
from core.exceptions import LLMResponseError, LLMConnectionError


logger = logging.getLogger(__name__)


class GLMClient(BaseLLMClient):
    """Клиент для GLM API (Cerebras)
    
    Используется как fallback при недоступности Gemini.
    Поддерживает Cerebras модели: qwen-3-32b, llama-4-scout-17b-16e-instruct
    """
    
    API_URL = "https://api.cerebras.ai/v1/chat/completions"
    DEFAULT_MODEL = "qwen-3-32b"
    DEFAULT_TIMEOUT = 120.0
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0
    
    def __init__(self, api_key: str, model_name: Optional[str] = None):
        model = model_name or self.DEFAULT_MODEL
        super().__init__(api_key, model)
        self._client: Optional[httpx.AsyncClient] = None
    
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
        json_mode: bool = False
    ) -> str:
        """Генерировать ответ с retry логикой
        
        Args:
            prompt: Текст запроса
            temperature: Креативность (0.0-1.0)
            json_mode: Если True, добавляет инструкцию вернуть JSON
        
        Returns:
            Текст ответа
            
        Raises:
            LLMConnectionError: При ошибке соединения после всех retry
            LLMResponseError: При пустом ответе
        """
        if json_mode:
            prompt = f"{prompt}\n\nRespond with valid JSON only."
        
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
                        "max_tokens": 8192
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                choices = data.get("choices", [])
                if not choices:
                    raise LLMResponseError("GLM returned no choices")
                
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if not content or not content.strip():
                    raise LLMResponseError("GLM returned empty response")
                
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
