#!/usr/bin/env python3
"""
Droid Stream Formatter - форматує JSON вивід Droid в читабельний текст

Використовується через pipe: droid | tee log.json | python3 droid_stream_fmt.py
"""
import sys
import json
import shutil

# Кольори ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[90m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"

def print_separator():
    """Друкує горизонтальну лінію на всю ширину терміналу"""
    cols, _ = shutil.get_terminal_size(fallback=(80, 24))
    print(f"{GRAY}{'─' * cols}{RESET}")

def format_line(line):
    """Форматує одну строку JSON в читабельний текст"""
    line = line.strip()
    if not line:
        return

    try:
        event = json.loads(line)
        evt_type = event.get("type")
        
        # 1. System events (показуємо тільки init)
        if evt_type == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                model = event.get("model", "unknown")
                print(f"{GRAY}⚙️  Droid initialized ({model}){RESET}")
            return

        # 2. User Message (пропускаємо, бо вже показали задачу)
        if evt_type == "message" and event.get("role") == "user":
            return

        # 3. Thinking (Assistant Message)
        if evt_type == "message" and event.get("role") == "assistant":
            content = event.get("text", "")
            if content and len(content) > 10:
                # Обрізаємо дуже довгі думки
                if len(content) > 150:
                    content = content[:150] + "..."
                print(f"\n{YELLOW}💭 {content}{RESET}")

        # 4. Tool Call (найважливіше!)
        elif evt_type == "tool_call":
            tool = event.get("toolName", "Unknown")
            params = event.get("parameters", {})
            
            # Форматуємо параметри залежно від інструменту
            if tool == "Execute":
                cmd = params.get("command", "")
                print(f"{BLUE}🔧 Виконую:{RESET} {CYAN}{cmd}{RESET}")
            elif tool == "Read":
                path = params.get("file_path", "")
                print(f"{BLUE}📖 Читаю:{RESET} {path}")
            elif tool == "Edit":
                path = params.get("file_path", "")
                print(f"{BLUE}✏️  Редагую:{RESET} {path}")
            elif tool == "Create":
                path = params.get("file_path", "")
                print(f"{BLUE}📄 Створюю:{RESET} {path}")
            elif tool == "Grep":
                pattern = params.get("pattern", "")
                print(f"{BLUE}🔍 Шукаю:{RESET} {pattern}")
            elif tool == "Glob":
                patterns = params.get("patterns", [])
                print(f"{BLUE}🔍 Шукаю файли:{RESET} {', '.join(patterns)}")
            elif tool == "LS":
                path = params.get("directory_path", ".")
                print(f"{BLUE}📁 Переглядаю:{RESET} {path}")
            else:
                # Інші інструменти - показуємо коротко
                params_str = str(params)[:100]
                if len(str(params)) > 100:
                    params_str += "..."
                print(f"{BLUE}🔧 {tool}:{RESET} {params_str}")

        # 5. Tool Result
        elif evt_type == "tool_result":
            is_error = event.get("isError", False)
            value = event.get("value", "")
            
            if is_error:
                # Помилка - показуємо червоним
                err_str = str(value)[:200]
                if len(str(value)) > 200:
                    err_str += "..."
                print(f"{RED}   ❌ Помилка: {err_str}{RESET}")
            else:
                # Успіх - показуємо коротко сірим
                res_str = str(value)
                if len(res_str) > 150:
                    res_str = res_str[:150].replace('\n', ' ') + "..."
                else:
                    res_str = res_str.replace('\n', ' ')
                print(f"{GRAY}   ✅ {res_str}{RESET}")

        # 6. Completion (фінал)
        elif evt_type == "completion":
            duration = event.get("durationMs", 0) / 1000
            num_turns = event.get("numTurns", 0)
            final_text = event.get("finalText", "")
            
            print()
            print_separator()
            print(f"{GREEN}{BOLD}✅ ЗАВЕРШЕНО{RESET} за {duration:.1f}с ({num_turns} кроків)")
            print_separator()
            
            # Показуємо фінальний текст якщо є
            if final_text and len(final_text) < 500:
                print(f"\n{final_text}\n")

        # 7. Error
        elif evt_type == "error":
            msg = event.get("message", "Unknown error")
            print(f"\n{RED}{BOLD}❌ ПОМИЛКА:{RESET} {msg}\n")

    except json.JSONDecodeError:
        # Якщо це не JSON, просто виводимо як є (системний вивід bash)
        if line and not line.startswith('{'):
            print(f"{GRAY}{line}{RESET}")

def main():
    """Читає stdin строка за строкою і форматує"""
    try:
        for line in sys.stdin:
            format_line(line)
            # Важливо робити flush щоб текст з'являвся миттєво
            sys.stdout.flush()
    except KeyboardInterrupt:
        # Graceful exit при Ctrl+C
        pass
    except BrokenPipeError:
        # Якщо pipe закрився - нормально виходимо
        pass

if __name__ == "__main__":
    main()
