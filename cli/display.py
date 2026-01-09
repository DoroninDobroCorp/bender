"""
Display - режимы отображения (visible/silent)

Visible: все детали, мысли Bender, output Droid
Silent: только прогресс и результат
"""

from enum import Enum
from typing import Optional
import sys


class DisplayMode(str, Enum):
    VISIBLE = "visible"
    SILENT = "silent"


class Colors:
    """ANSI цвета для терминала"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


class Display:
    """Класс для вывода информации в терминал"""
    
    def __init__(self, mode: DisplayMode = DisplayMode.VISIBLE, use_colors: bool = True):
        self.mode = mode
        self.use_colors = use_colors and sys.stdout.isatty()
    
    def _color(self, text: str, color: str) -> str:
        """Добавить цвет к тексту"""
        if not self.use_colors:
            return text
        return f"{color}{text}{Colors.RESET}"
    
    def header(self, text: str):
        """Заголовок"""
        line = "=" * 60
        print()
        print(self._color(line, Colors.CYAN))
        print(self._color(f"  {text}", Colors.BOLD + Colors.CYAN))
        print(self._color(line, Colors.CYAN))
        print()
    
    def separator(self):
        """Разделитель"""
        print(self._color("-" * 60, Colors.DIM))
    
    def info(self, text: str):
        """Информационное сообщение"""
        print(self._color(f"  {text}", Colors.WHITE))
    
    def success(self, text: str):
        """Успешное сообщение"""
        print(self._color(f"  ✓ {text}", Colors.GREEN))
    
    def warning(self, text: str):
        """Предупреждение"""
        print(self._color(f"  ⚠ {text}", Colors.YELLOW))
    
    def error(self, text: str):
        """Ошибка"""
        print(self._color(f"  ✗ {text}", Colors.RED))
    
    def progress(self, text: str):
        """Прогресс (показывается в обоих режимах)"""
        if self.mode == DisplayMode.SILENT:
            # В silent режиме - краткий вывод
            print(self._color(f"→ {text}", Colors.DIM))
        else:
            # В visible режиме - полный вывод
            print(self._color(f"  → {text}", Colors.BLUE))
    
    def step_start(self, step_id: int, step_name: str):
        """Начало шага"""
        print()
        print(self._color(f"  Step {step_id}/6: {step_name}", Colors.BOLD + Colors.MAGENTA))
        print(self._color("  " + "-" * 40, Colors.DIM))
    
    def step_complete(self, step_id: int, iterations: int):
        """Завершение шага"""
        print(self._color(f"  ✓ Step {step_id} complete ({iterations} iterations)", Colors.GREEN))
    
    def iteration(self, step_id: int, iteration: int, confirmations: int):
        """Информация об итерации"""
        if self.mode == DisplayMode.VISIBLE:
            print(self._color(f"    Iteration {iteration}, confirmations: {confirmations}/2", Colors.DIM))
    
    def droid_output(self, output: str, max_lines: int = 20):
        """Вывод от Droid (только в visible режиме)"""
        if self.mode != DisplayMode.VISIBLE:
            return
        
        lines = output.strip().split('\n')
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
        
        print(self._color("    Droid:", Colors.CYAN))
        for line in lines:
            print(self._color(f"    │ {line}", Colors.DIM))
    
    def bender_thought(self, thought: str):
        """Мысль Bender (только в visible режиме)"""
        if self.mode != DisplayMode.VISIBLE:
            return
        
        print(self._color(f"    🤖 Bender: {thought}", Colors.YELLOW))
    
    def git_action(self, action: str):
        """Git действие"""
        if self.mode == DisplayMode.VISIBLE:
            print(self._color(f"    📦 Git: {action}", Colors.BLUE))
        else:
            print(self._color(f"→ Git: {action}", Colors.DIM))
    
    def escalation(self, reason: str):
        """Эскалация к человеку"""
        print()
        print(self._color("  " + "!" * 60, Colors.BG_RED + Colors.WHITE))
        print(self._color(f"  HUMAN INTERVENTION REQUIRED", Colors.BG_RED + Colors.WHITE + Colors.BOLD))
        print(self._color(f"  {reason}", Colors.RED))
        print(self._color("  " + "!" * 60, Colors.BG_RED + Colors.WHITE))
        print()
    
    def final_report(self, stats: dict):
        """Финальный отчет"""
        print()
        self.separator()
        print(self._color("  FINAL REPORT", Colors.BOLD))
        self.separator()
        
        for key, value in stats.items():
            print(self._color(f"  {key}: {value}", Colors.WHITE))
        
        self.separator()
