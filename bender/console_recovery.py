"""
Console recovery and nudge logic.

Detects terminal/console crashes and gently pushes the CLI to continue.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, List, Pattern, Any

logger = logging.getLogger(__name__)


@dataclass
class ConsoleRecoveryConfig:
    """Settings for console recovery flow"""
    max_attempts: int = 2
    cooldown_seconds: float = 30.0
    continue_delay_seconds: float = 60.0
    initial_message: str = "О боже, ошибка! Не закрывай терминал. Давайте начнем всё сначала."
    continue_message: str = "Продолжай"


class ConsoleRecovery:
    """Detect and recover from console errors by nudging the CLI"""

    DEFAULT_ERROR_PATTERNS = [
        r"(terminal|console|tty).*(error|crash|died|closed|terminated)",
        r"session .* (terminated|closed|died|crashed)",
        r"tmux:.*(no server|server exited|not running)",
        r"connection (reset|refused|closed|lost|aborted)",
        r"socket hang up",
        r"broken pipe",
        r"unexpected (eof|error)",
        r"segmentation fault|core dumped|panic",
        r"process .* exited with code",
        r"exit code [1-9]",
        r"error:\s*403|error:\s*429|rate limit",
        r"internal error|fatal error",
        r"ошибка|краш|вылет(ел|ела|ело)|соединение.*(сброшено|разорвано)",
    ]

    ENTER_PROMPT_PATTERNS = [
        r"press (enter|return|any key)",
        r"press any key to continue",
        r"нажмите (enter|return|любую клавишу)",
    ]

    def __init__(
        self,
        config: Optional[ConsoleRecoveryConfig] = None,
        error_patterns: Optional[List[str]] = None,
    ):
        self.config = config or ConsoleRecoveryConfig()
        patterns = error_patterns or self.DEFAULT_ERROR_PATTERNS
        self._error_res: List[Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._enter_res: List[Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in self.ENTER_PROMPT_PATTERNS]
        self._attempts = 0
        self._last_attempt_ts = 0.0

    def reset(self) -> None:
        """Reset attempt counters"""
        self._attempts = 0
        self._last_attempt_ts = 0.0

    def attempts_left(self) -> int:
        return max(0, self.config.max_attempts - self._attempts)

    def detect_issue(self, output: str) -> Optional[str]:
        """Return a short reason if output looks like a console crash"""
        if not output:
            return None

        lines = [line.strip() for line in output.splitlines() if line.strip()]
        recent = lines[-50:] if len(lines) > 50 else lines

        for line in reversed(recent):
            for pattern in self._error_res:
                if pattern.search(line):
                    return line[:160]
        return None

    def _needs_enter(self, output: str) -> bool:
        for pattern in self._enter_res:
            if pattern.search(output):
                return True
        return False

    async def attempt_recovery(
        self,
        worker: Any,
        on_status: Optional[Callable[[str], Awaitable[None]]],
        reason: str,
        output: str,
    ) -> bool:
        """Try to recover the console by nudging it.

        Returns True if output changed after nudges, False otherwise.
        """
        now = time.time()
        if self._attempts >= self.config.max_attempts:
            return False
        if now - self._last_attempt_ts < self.config.cooldown_seconds:
            return False

        self._attempts += 1
        self._last_attempt_ts = now

        if on_status:
            await on_status(f"⚠️ Console issue detected. Nudging terminal... ({self._attempts}/{self.config.max_attempts})")
            await on_status(f"   Причина: {reason[:120]}")

        # Ensure session is alive before sending input
        try:
            if hasattr(worker, "is_session_alive"):
                alive = await worker.is_session_alive()
                if not alive:
                    return False
        except Exception:
            pass

        before_hash = hash(output[-1000:]) if output else 0

        try:
            # If it asks to press Enter, do it first
            if self._needs_enter(output):
                await worker.send_input("")
                await asyncio.sleep(1)

            # Human-like "push" sequence
            await worker.send_input(self.config.initial_message)
            await asyncio.sleep(2)
            await worker.send_input(self.config.continue_message)
            await asyncio.sleep(self.config.continue_delay_seconds)
            await worker.send_input(self.config.continue_message)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Console recovery failed to send input: {e}")
            return False

        # Check if output changed
        try:
            new_output = await worker.capture_output()
        except Exception:
            new_output = ""

        after_hash = hash(new_output[-1000:]) if new_output else 0
        return after_hash != before_hash
