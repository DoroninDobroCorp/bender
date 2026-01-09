"""
GLM API Client (Cerebras) - Fallback для Gemini
"""

import asyncio
import json
import re
from typing import Optional, Dict, Any

import httpx


class GLMClient:
    """Клиент для GLM API (Cerebras)
    
    Используется как fallback при недоступности Gemini.
    ВАЖНО: Llama модели ЗАПРЕЩЕНЫ!
    """
    
    API_URL = "https://api.cerebras.ai/v1/chat/completions"
    DEFAULT_MODEL = "glm-4-plus"  # GLM-4.6+
    
    def __init__(self, api_key: str, model_name: str = None):
        self.api_key = api_key
        self.model_name = model_name or self.DEFAULT_MODEL
        
        # Проверка что не Llama
        if "llama" in self.model_name.lower():
            raise ValueError("Llama models are FORBIDDEN! Use GLM only.")
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Генерировать ответ
        
        Args:
            prompt: Текст запроса
            temperature: Креативность (0.0-1.0)
            json_mode: Если True, добавляет инструкцию вернуть JSON
        
        Returns:
            Текст ответа
        """
        if json_mode:
            prompt = f"{prompt}\n\nRespond with valid JSON only."
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 8192
                }
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ"""
        response = await self.generate(prompt, temperature, json_mode=True)
        return self._parse_json(response)
    
    def _parse_json(self, text: str) -> Dict[str, Any]:
        """Извлечь JSON из ответа"""
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        
        return json.loads(text)
