"""
Gemini API Client

ВАЖНО: Разрешены ТОЛЬКО две модели:
- gemini-2.0-flash-exp (быстрая, для fallback)
- gemini-2.5-pro-preview-06-05 (продвинутая)

Другие модели СТРОГО ЗАПРЕЩЕНЫ!
"""

import asyncio
import logging
from typing import Optional, List, Dict
import httpx

logger = logging.getLogger(__name__)

# РАЗРЕШЁННЫЕ МОДЕЛИ - другие использовать ЗАПРЕЩЕНО!
# Только gemini-3-flash и gemini-2.5-pro!
ALLOWED_MODELS = [
    "gemini-3-flash-preview",  # Gemini 3 Flash (быстрая)
    "gemini-2.5-pro",          # Gemini 2.5 Pro (продвинутая)
]

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClient:
    """Простой клиент для Gemini API"""
    
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash-exp"):
        if model not in ALLOWED_MODELS:
            raise ValueError(
                f"Модель '{model}' запрещена! "
                f"Разрешены только: {ALLOWED_MODELS}"
            )
        
        self.api_key = api_key
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client
    
    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> str:
        """Сгенерировать ответ"""
        
        url = f"{BASE_URL}/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }
        
        client = await self._get_client()
        
        try:
            response = await client.post(url, json=payload)
            
            if response.status_code == 429:
                raise Exception("429: Rate limit exceeded")
            
            response.raise_for_status()
            data = response.json()
            
            # Извлекаем текст из ответа
            if "candidates" in data and data["candidates"]:
                candidate = data["candidates"][0]
                if "content" in candidate and "parts" in candidate["content"]:
                    parts = candidate["content"]["parts"]
                    if parts and "text" in parts[0]:
                        return parts[0]["text"]
            
            # Если формат неожиданный
            logger.warning(f"Unexpected Gemini response: {data}")
            raise Exception("Invalid Gemini response format")
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini HTTP error: {e.response.status_code}")
            raise Exception(f"Gemini error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            raise


class GeminiKeyRotator:
    """Ротация ключей для Gemini"""
    
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.current = 0
        self.failed: Dict[str, float] = {}
    
    def get_key(self) -> Optional[str]:
        if not self.keys:
            return None
        
        import time
        now = time.time()
        
        for _ in range(len(self.keys)):
            key = self.keys[self.current]
            self.current = (self.current + 1) % len(self.keys)
            
            if key in self.failed:
                if now - self.failed[key] < 30:  # 30s cooldown
                    continue
                del self.failed[key]
            
            return key
        
        return None
    
    def mark_failed(self, key: str):
        import time
        self.failed[key] = time.time()
