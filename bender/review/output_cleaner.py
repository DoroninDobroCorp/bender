"""Очистка вывода worker'ов — ANSI, JSON events, Bender prompt headers."""

import json
import re


# ANSI escape sequences
_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text: str) -> str:
    """Удалить ANSI escape-коды из текста."""
    return _ANSI_RE.sub('', text or '')


def extract_human_response(full_output: str) -> str:
    """Извлечь человеческий ответ из RAW output worker'а.

    Два формата:
    1. Обычный текст (copilot/codex) — удаляем ANSI и JSON-хвосты
    2. JSON events от droid (stream-json) — собираем текст из message/completion events
    """
    if not full_output:
        return ""

    text = strip_ansi(full_output)
    lines = text.split('\n')

    # Если ≥3 строки начинаются с {"type": — это JSON events от droid
    json_event_lines = sum(1 for line in lines if line.strip().startswith('{"type":'))

    if json_event_lines >= 3:
        human_parts: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                event_type = event.get('type')
                if event_type == 'message' and event.get('role') == 'assistant':
                    msg_text = event.get('text', '')
                    if msg_text:
                        human_parts.append(msg_text)
                elif event_type == 'completion':
                    final_text = event.get('finalText', '')
                    if final_text:
                        human_parts.append(final_text)
            except json.JSONDecodeError:
                if line.strip() and not line.startswith('{'):
                    human_parts.append(line)
        return '\n'.join(human_parts).strip()

    # Обычный текст — обрезаем JSON-логи в конце
    if '{"type":' in text:
        idx = text.rfind('{"type":')
        if idx > len(text) - 500:
            text = text[:idx]

    return text.strip()


def strip_prompt_header(text: str) -> str:
    """Убрать Bender prompt-header из начала вывода, если он есть."""
    if "🤖 BENDER →" not in text:
        return text
    parts = text.split("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if len(parts) >= 3:
        return "━━━".join(parts[2:]).strip()
    return text
