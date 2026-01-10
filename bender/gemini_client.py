"""
Gemini API Client
"""

import asyncio
import logging
from typing import Dict, Any, Optional

from google import genai
from google.genai import types

from .base_client import BaseLLMClient, LLMProvider
from .utils import parse_json_response, JSONParseError
from core.exceptions import LLMResponseError, LLMConnectionError


logger = logging.getLogger(__name__)


class GeminiClient(BaseLLMClient):
    """Клиент для Gemini API
    
    Поддерживаемые модели (НЕ МЕНЯТЬ!):
    - gemini-2.5-pro (default)
    - gemini-3-pro
    - gemini-3-flash
    
    Supports async context manager:
        async with GeminiClient(api_key) as client:
            response = await client.generate(...)
    """
    
    ALLOWED_MODELS = ["gemini-2.5-pro", "gemini-3-pro", "gemini-3-flash"]
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0
    
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-pro"):
        if model_name not in self.ALLOWED_MODELS:
            raise ValueError(f"Model {model_name} not allowed. Use: {self.ALLOWED_MODELS}")
        
        super().__init__(api_key, model_name)
        self._client: Optional[genai.Client] = None
    
    @property
    def provider(self) -> LLMProvider:
        """Return the provider type"""
        return LLMProvider.GEMINI
    
    def _get_client(self) -> genai.Client:
        """Get or create the Gemini client (lazy initialization)"""
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client
    
    async def close(self):
        """Close the client and release resources"""
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
        """Генерировать ответ с retry логикой"""
        if json_mode:
            prompt = f"{prompt}\n\nRespond with valid JSON only."
        
        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = await asyncio.to_thread(
                    self._generate_sync,
                    prompt,
                    temperature
                )
                return response
            except Exception as e:
                last_error = e
                logger.warning(f"Gemini generate error (attempt {attempt}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * (2 ** (attempt - 1)))
        
        raise last_error
    
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
            raise JSONParseError(f"Failed to parse JSON from Gemini response: {e}", raw_text=response)
    
    def _generate_sync(self, prompt: str, temperature: float) -> str:
        """Синхронная генерация"""
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                top_p=0.95,
                top_k=40,
                max_output_tokens=8192,
            )
        )
        
        if response.text is None or response.text.strip() == "":
            raise LLMResponseError("Gemini returned empty response")
        
        return response.text
