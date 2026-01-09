"""
Gemini API Client
"""

import asyncio
import json
import re
from typing import Optional, Dict, Any

from google import genai
from google.genai import types


class GeminiClient:
    """Клиент для Gemini API
    
    Поддерживаемые модели (НЕ МЕНЯТЬ!):
    - gemini-2.5-pro (default)
    - gemini-3-pro
    - gemini-3-flash
    """
    
    ALLOWED_MODELS = ["gemini-2.5-pro", "gemini-3-pro", "gemini-3-flash"]
    
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-pro"):
        if model_name not in self.ALLOWED_MODELS:
            raise ValueError(f"Model {model_name} not allowed. Use: {self.ALLOWED_MODELS}")
        
        self.api_key = api_key
        self.model_name = model_name
        
        self.client = genai.Client(api_key=api_key)
    
    async def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False
    ) -> str:
        """Генерировать ответ"""
        if json_mode:
            prompt = f"{prompt}\n\nRespond with valid JSON only."
        
        response = await asyncio.to_thread(
            self._generate_sync,
            prompt,
            temperature
        )
        
        return response
    
    async def generate_json(
        self,
        prompt: str,
        temperature: float = 0.3
    ) -> Dict[str, Any]:
        """Генерировать JSON ответ"""
        response = await self.generate(prompt, temperature, json_mode=True)
        return self._parse_json(response)
    
    def _generate_sync(self, prompt: str, temperature: float) -> str:
        """Синхронная генерация"""
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                top_p=0.95,
                top_k=40,
                max_output_tokens=8192,
            )
        )
        
        return response.text
    
    def _parse_json(self, text: str) -> Dict[str, Any]:
        """Извлечь JSON из ответа"""
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        
        return json.loads(text)
