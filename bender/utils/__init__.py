"""
Shared utilities for Bender components
"""

import json
import re
import logging
from typing import Dict, Any, Optional, Union, List

from core.exceptions import JSONParseError


logger = logging.getLogger(__name__)


def parse_json_response(text: str) -> Any:
    """Extract JSON from LLM response (supports objects, arrays and markdown blocks)
    
    Args:
        text: Raw LLM response text
        
    Returns:
        Parsed JSON (dict or list)
        
    Raises:
        JSONParseError: If no valid JSON found
    """
    if not text or not text.strip():
        raise JSONParseError("Empty response text", raw_text=text or "")
    
    # Try markdown code block first (object or array)
    json_match = re.search(r'```json\s*([\{\[].*?[\}\]])\s*```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse JSON from markdown block: {e}")
    
    # Find JSON by matching balanced braces/brackets
    json_str = _find_json_object(text) or _find_json_array(text)
    if json_str:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse extracted JSON: {e}")
    
    # Last resort: try parsing entire text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise JSONParseError(f"No valid JSON found in response: {e}", raw_text=text)


def _find_json_object(s: str) -> Optional[str]:
    """Find complete JSON object with balanced braces"""
    start = s.find('{')
    if start == -1:
        return None
    
    depth = 0
    in_string = False
    escape = False
    
    for i, c in enumerate(s[start:], start):
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return s[start:i+1]
    
    return None


def _find_json_array(s: str) -> Optional[str]:
    """Find complete JSON array with balanced brackets"""
    start = s.find('[')
    if start == -1:
        return None
    
    depth = 0
    in_string = False
    escape = False
    
    for i, c in enumerate(s[start:], start):
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return s[start:i+1]
    
    return None
