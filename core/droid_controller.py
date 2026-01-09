"""
Droid Controller - управление Factory Droid через tmux
Базируется на VibeCoder_Dream с упрощениями (без переключения моделей)
"""

import asyncio
import subprocess
import time
import re
import uuid
from typing import Optional, List, Dict
from pathlib import Path
from datetime import datetime


class DroidController:
    """Контроллер для Factory Droid через tmux"""
    
    def __init__(
        self,
        project_path: str,
        droid_binary: str = "droid",
        log_dir: str = "logs",
        idle_timeout: int = 120,
        check_interval: float = 2.0
    ):
        self.project_path = Path(project_path)
        self.droid_binary = droid_binary
        self.log_dir = Path(log_dir)
        self.idle_timeout = idle_timeout
        self.check_interval = check_interval
        
        self.session_name = f"parser-maker-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"droid_{timestamp}_{self.session_name}.log"
        
        self.conversation_history: List[Dict] = []
        self.last_output = ""
        self.last_output_length = 0
        
    async def start(self) -> bool:
        """Запустить Droid в tmux сессии"""
        if not self.project_path.exists():
            raise ValueError(f"Project not found: {self.project_path}")
        
        self.log_dir.mkdir(exist_ok=True)
        self._log(f"=== DROID SESSION START ===\n")
        self._log(f"Project: {self.project_path}\n")
        self._log(f"Session: {self.session_name}\n\n")
        
        # Убить существующую сессию если есть
        result = subprocess.run(
            ['tmux', 'has-session', '-t', self.session_name],
            capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(['tmux', 'kill-session', '-t', self.session_name])
            await asyncio.sleep(1)
        
        # Создать новую сессию
        subprocess.run(
            ['tmux', 'new-session', '-d', '-s', self.session_name],
            check=True
        )
        
        # Перейти в директорию проекта
        subprocess.run(
            ['tmux', 'send-keys', '-t', self.session_name, f'cd {self.project_path}', 'Enter'],
            check=True
        )
        await asyncio.sleep(1)
        
        # Запустить droid
        subprocess.run(
            ['tmux', 'send-keys', '-t', self.session_name, self.droid_binary, 'Enter'],
            check=True
        )
        await asyncio.sleep(5)
        
        # Обработать начальные диалоги (VSCode Extension и т.д.)
        initial_output = self._capture_pane()
        
        if "VSCode Extension" in initial_output:
            subprocess.run(
                ['tmux', 'send-keys', '-t', self.session_name, 'Escape'],
                check=True
            )
            await asyncio.sleep(2)
            initial_output = self._capture_pane()
            
            if "Would you like to install" in initial_output:
                subprocess.run(
                    ['tmux', 'send-keys', '-t', self.session_name, 'n'],
                    check=True
                )
                await asyncio.sleep(1)
        
        self._log(f"Initial output:\n{initial_output}\n\n")
        self.last_output = initial_output
        self.last_output_length = len(initial_output)
        
        return True
    
    async def send(self, message: str, timeout: Optional[int] = None) -> str:
        """Отправить команду Droid и дождаться ответа"""
        if timeout is None:
            timeout = self.idle_timeout
            
        text = message.strip()
        self._log(f"\n{'='*60}\nUSER: {text}\n{'='*60}\n")
        
        self.conversation_history.append({
            "role": "user",
            "content": text,
            "timestamp": time.time()
        })
        
        # Отправить команду
        subprocess.run(
            ['tmux', 'send-keys', '-t', self.session_name, text, 'Enter'],
            check=True
        )
        
        # Ждать ответа
        response = await self._wait_for_response(timeout)
        
        self._log(f"\nDROID:\n{response}\n")
        
        self.conversation_history.append({
            "role": "assistant",
            "content": response,
            "timestamp": time.time()
        })
        
        return response
    
    async def _wait_for_response(self, timeout: int) -> str:
        """Ждать ответ от Droid, вернуть дельту (новый output)"""
        start_time = time.time()
        last_output = ""
        stable_count = 0
        initial_length = self.last_output_length
        last_timer = None
        timer_stable_count = 0
        
        while True:
            if time.time() - start_time > timeout:
                break
            
            current_output = self._capture_pane()
            self.last_output = current_output
            
            # Проверить активно ли выполняется команда
            is_executing = self._is_executing(current_output)
            
            # Проверить таймер работы [⏱ XXmXXs]
            current_timer = self._extract_work_timer(current_output)
            if current_timer:
                if current_timer == last_timer:
                    timer_stable_count += 1
                else:
                    timer_stable_count = 0
                    stable_count = 0
                last_timer = current_timer
            
            if is_executing:
                stable_count = 0
                timer_stable_count = 0
            
            if current_output == last_output:
                stable_count += 1
                # 3 стабильных проверки, таймер не меняется, не в процессе выполнения
                if stable_count >= 3 and (not current_timer or timer_stable_count >= 3) and not is_executing:
                    break
            else:
                stable_count = 0
                last_output = current_output
            
            # Проверить approval request
            if self._has_approval_request(current_output):
                break
            
            await asyncio.sleep(self.check_interval)
        
        # Вернуть только новую часть (дельту)
        current_length = len(current_output)
        if current_length > initial_length:
            delta = current_output[initial_length:]
            self.last_output_length = current_length
            return delta
        else:
            self.last_output_length = current_length
            return current_output
    
    def _capture_pane(self) -> str:
        """Захватить output из tmux pane"""
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', self.session_name, '-p', '-S', '-'],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    
    def _extract_work_timer(self, output: str) -> Optional[str]:
        """Извлечь таймер работы [⏱ XXmXXs]"""
        match = re.search(r'\[⏱\s*(\d+m\s*\d+s)\]', output)
        if match:
            return match.group(1).strip()
        return None
    
    def _is_executing(self, output: str) -> bool:
        """Проверить активно ли droid выполняет команду"""
        patterns = [
            r'Executing\.\.\.',
            r'Running command',
            r'In progress',
            r'EXECUTE',
            r'⏳'
        ]
        for pattern in patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return True
        return False
    
    def _has_approval_request(self, output: str) -> bool:
        """Проверить есть ли approval request"""
        patterns = [
            r'\(Yes/No\)',
            r'\(Y/n\)',
            r'Allow this',
            r'Do you approve',
            r'Continue\?'
        ]
        for pattern in patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return True
        return False
    
    def has_approval_request(self) -> bool:
        """Публичный метод для проверки approval request в текущем output"""
        return self._has_approval_request(self.last_output)
    
    async def approve(self, response: str = "Yes"):
        """Отправить подтверждение на approval request"""
        self._log(f"\nAUTO-APPROVAL: {response}\n")
        subprocess.run(
            ['tmux', 'send-keys', '-t', self.session_name, response, 'Enter'],
            check=True
        )
        await asyncio.sleep(2)
    
    async def new_chat(self) -> str:
        """Открыть новый чат через /new"""
        self._log(f"\n{'='*60}\nNEW CHAT\n{'='*60}\n")
        
        subprocess.run(
            ['tmux', 'send-keys', '-t', self.session_name, '/new', 'Enter'],
            check=True
        )
        await asyncio.sleep(2)
        
        output = self._capture_pane()
        self.last_output = output
        self.last_output_length = len(output)
        self.conversation_history = []
        
        self._log(f"New chat opened:\n{output}\n")
        return output
    
    async def stop(self):
        """Остановить Droid и закрыть tmux сессию"""
        self._log(f"\n{'='*60}\nSESSION END\n{'='*60}\n")
        
        final_output = self._capture_pane()
        self._log(f"Final output:\n{final_output}\n")
        
        try:
            subprocess.run(
                ['tmux', 'send-keys', '-t', self.session_name, 'exit', 'Enter']
            )
            await asyncio.sleep(2)
        except:
            pass
        
        subprocess.run(['tmux', 'kill-session', '-t', self.session_name])
    
    def is_running(self) -> bool:
        """Проверить запущена ли tmux сессия"""
        result = subprocess.run(
            ['tmux', 'has-session', '-t', self.session_name],
            capture_output=True
        )
        return result.returncode == 0
    
    def get_current_output(self) -> str:
        """Получить текущий output"""
        return self._capture_pane()
    
    def get_conversation_history(self) -> List[Dict]:
        """Получить историю разговора"""
        return self.conversation_history.copy()
    
    def _log(self, message: str):
        """Записать в лог файл"""
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(message)
