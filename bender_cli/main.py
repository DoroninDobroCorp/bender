#!/usr/bin/env python3
"""
Bender CLI - AI Task Supervisor

"""

import asyncio
import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

BENDER_ROOT = Path(__file__).parent.parent
if str(BENDER_ROOT) not in sys.path:
    sys.path.insert(0, str(BENDER_ROOT))

from core.config import load_config
from core.logging_config import setup_logging


def _should_run_global_cleanup() -> bool:
    """Return True if it's safe to kill global bender processes.

    Skips cleanup when running in parallel to avoid killing other sessions.
    """
    if os.getenv("BENDER_ALLOW_PARALLEL", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("BENDER_SKIP_GLOBAL_CLEANUP", "").lower() in ("1", "true", "yes"):
        return False
    try:
        patterns = ["bmad-bender run", "bender run"]
        current_pid = os.getpid()
        for pattern in patterns:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                pids = [int(p) for p in result.stdout.split() if p.isdigit()]
                if any(pid != current_pid for pid in pids):
                    return False
    except Exception:
        # If detection fails, keep old behavior
        return True
    return True


def _atexit_cleanup():
    """Cleanup при выходе из bender - убиваем зависшие процессы"""
    if not _should_run_global_cleanup():
        return
    try:
        from bender.workers.copilot import cleanup_orphaned_processes
        result = cleanup_orphaned_processes()
        if result.get("total_killed", 0) > 0 or result.get("total_closed", 0) > 0:
            # Печатаем только если что-то почистили
            sys.stderr.write(f"\n🧹 Bender cleanup: killed {result.get('total_killed', 0)} processes, closed {result.get('total_closed', 0)} windows\n")
    except Exception:
        pass  # Тихо игнорируем ошибки при cleanup


# NOTE: atexit cleanup регистрируется в group callback (Story 48.4),
# чтобы не срабатывать при import (для lazy loading из lev bender).
_atexit_registered = False


def clean_surrogates(text: str) -> str:
    """Remove surrogate characters that can't be encoded in UTF-8 (from broken terminal/tmux input)"""
    if not text:
        return text
    try:
        return text.encode('utf-8', errors='replace').decode('utf-8')
    except Exception:
        return ''.join(char for char in text if not (0xD800 <= ord(char) <= 0xDFFF))


# Bender ASCII Art
BENDER_ASCII = r"""
    ╭──────────────────────────────────╮
    │  ( )  ___________  ( )           │
    │   ║  /           \  ║            │
    │   ║ |  ⚫     ⚫  | ║            │
    │      |      ▽      |             │
    │      |  ═══════   |              │
    │       \_________/                │
    │          ║   ║                   │
    │    ┌─────╨───╨─────┐             │
    │    │   B E N D E R │             │
    │    └───────────────┘             │
    ╰──────────────────────────────────╯
       "Bite my shiny metal CLI!"
"""


# Глобальные ссылки для graceful shutdown
_task_manager = None
_shutdown_event: Optional[asyncio.Event] = None


def bender_echo(message: str) -> None:
    """Цветной вывод от Bender'а - выделяется от обычных логов"""
    # Определяем тип сообщения и цвет
    if message.startswith("✅") or "completed" in message.lower() or "done" in message.lower():
        # Успех - зелёный
        prefix = click.style("🤖 BENDER", fg="green", bold=True)
    elif message.startswith("❌") or "error" in message.lower() or "failed" in message.lower():
        # Ошибка - красный
        prefix = click.style("🤖 BENDER", fg="red", bold=True)
    elif message.startswith("⏳") or "working" in message.lower() or "waiting" in message.lower():
        # В процессе - жёлтый
        prefix = click.style("🤖 BENDER", fg="yellow", bold=True)
    elif "===" in message or "Iteration" in message or "Starting" in message:
        # Новая итерация/этап - cyan
        prefix = click.style("🤖 BENDER", fg="cyan", bold=True)
    elif "Decision" in message or "Found" in message:
        # Решения - magenta
        prefix = click.style("🤖 BENDER", fg="magenta", bold=True)
    else:
        # Обычный статус - синий
        prefix = click.style("🤖 BENDER", fg="blue", bold=True)
    
    click.echo(f"{prefix} {message}")


def handle_shutdown(signum, frame):
    """Handle Ctrl+C - cleanup processes before exit"""
    if _task_manager:
        _task_manager.request_stop()
    if _shutdown_event:
        _shutdown_event.set()
    click.echo("\n⚠️  Stopping and cleaning up...")
    
    # Сразу запускаем cleanup
    if _should_run_global_cleanup():
        try:
            from bender.workers.copilot import cleanup_orphaned_processes
            cleanup_orphaned_processes()
        except Exception:
            pass
    
    # Force exit on second Ctrl+C
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))


@click.group(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--debug', is_flag=True, help='Enable debug logging')
@click.pass_context
def cli(ctx, debug):
    """Bender - AI Task Supervisor.

    Supervises AI tools (copilot, droid, codex) to complete your tasks.

    \b
    Examples:
        lev bender run "Add OAuth authentication"
        lev bender run --droid "Fix typo in README"
        lev bender run --codex "Find memory leak in worker.py"
        lev bender status
        lev bender attach
    """
    global _atexit_registered
    ctx.ensure_object(dict)
    ctx.obj['debug'] = debug

    # Register atexit cleanup only when bender is actually used
    if not _atexit_registered:
        atexit.register(_atexit_cleanup)
        _atexit_registered = True


@cli.command()
@click.argument('task', required=False, default=None)
@click.option('--droid', is_flag=True, help='Force droid worker (simple tasks)')
@click.option('--opus', is_flag=True, help='Force opus/copilot worker (medium tasks)')
@click.option('--codex', is_flag=True, help='Force codex worker (complex tasks)')
@click.option('--auto', '-a', is_flag=True, default=True, help='Auto-select worker by complexity (default)')
@click.option('--interval', '-i', type=int, default=60, help='Log check interval in seconds')
@click.option('--simple', '-s', is_flag=True, help='Skip clarification and verification')
@click.option('--visible', '-v', is_flag=True, help='Show terminal windows (tmux)')
@click.option('--review-loop', '-l', is_flag=True, help='Iterative copilot→codex loop until clean')
@click.option('--copilot-review', '-c', is_flag=True, help='Use copilot instead of codex for review (saves codex limits)')
@click.option('--droid-mode', '-d', count=True, help='Use droid: -d = execution only, -dd = execution AND review (fastest!)')
@click.option('--party', '-P', is_flag=True, help='BMAD Party 10-role scoring (98+ threshold)')
@click.option('--max-iterations', type=int, default=10, help='Max iterations for review loop')
@click.option('--continue-errors', '-C', type=str, default=None, help='Continue mode: comma-separated errors to fix first')
@click.option('--errors-interactive', '-E', is_flag=True, help='Enter errors interactively (line by line)')
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path')
@click.pass_context
def run(ctx, task, droid, opus, codex, auto, interval, simple, visible, review_loop, copilot_review, droid_mode, party, max_iterations, continue_errors, errors_interactive, project):
    """Run a task with Bender supervision
    
    TASK can be omitted - Bender will ask interactively.
    
    By default, Bender will:
    1. Analyze task complexity
    2. Auto-select worker (droid/opus/codex)
    3. Monitor and nudge if needed
    4. Verify completion
    
    Use --simple to skip analysis and verification.
    Use --droid or --codex to force a specific worker.
    Use --review-loop for iterative copilot→codex→copilot cycle.
    Use --copilot-review (-c) with --review-loop to use copilot for review.
    Use -d for droid execution (codex review), -dd for FULL DROID (droid-droid) - fastest!
    Use -E to enter errors interactively, or -C "errors" to pass directly.
    Use -v to show tmux terminal windows.
    
    Examples:
        bender run "Add OAuth authentication"
        bender run -lv               # Loop with visible terminal
        bender run -lvc              # Loop with copilot review  
        bender run -lvd              # Loop: droid exec, codex review
        bender run -lvdd             # Loop: droid-droid (FASTEST!)
        bender run -lvP              # Loop with BMAD Party scoring (10 roles)
        bender run -lvdP             # Droid exec + BMAD Party review
        bender run -lvE              # Loop + errors interactive
        bender run -lvc -C "bug1, bug2" "task"
    """
    # Deprecation warning (Story 50.2)
    click.echo('⚠️  Deprecated: use `lev -l "task"` instead of `lev bender run "task"`', err=True)

    # Interactive mode: ask for task if not provided
    if task is None:
        click.echo(BENDER_ASCII)
        click.echo("🤖 Bender Interactive Mode")
        click.echo()
        click.echo("📝 Enter your task (two empty lines to finish):")
        import sys
        lines = []
        empty_count = 0
        try:
            if sys.stdin.isatty():
                while True:
                    try:
                        line = input("   ")
                        if not line.strip():
                            empty_count += 1
                            if empty_count >= 2:
                                break
                        else:
                            empty_count = 0
                            lines.append(line)
                    except EOFError:
                        break
            else:
                lines = sys.stdin.read().strip().split('\n')
        except KeyboardInterrupt:
            click.echo("\n⚠️ Cancelled")
            return
        
        if not lines:
            click.echo("❌ No task provided")
            return
        
        task = clean_surrogates("\n".join(lines))
        click.echo()
    
    # Interactive errors mode: -E flag
    if errors_interactive:
        click.echo("🐛 Enter errors to fix (paste all at once, then Ctrl+D or empty line twice to finish):")
        import sys
        lines = []
        empty_count = 0
        try:
            # Try reading from stdin directly for multi-line paste support
            if sys.stdin.isatty():
                # Interactive terminal - read line by line
                while True:
                    try:
                        line = input("   ")
                        if not line.strip():
                            empty_count += 1
                            if empty_count >= 2:
                                break
                        else:
                            empty_count = 0
                            lines.append(line)
                    except EOFError:
                        break
            else:
                # Piped input
                lines = sys.stdin.read().strip().split('\n')
        except KeyboardInterrupt:
            pass
        if lines:
            # Join with newline to preserve structure, then clean up surrogates
            continue_errors = clean_surrogates("\n".join(lines))
        click.echo()
    
    # Очищаем task от битой кодировки (если передан как аргумент)
    if task:
        task = clean_surrogates(task)
    
    # Track if -E was used but no errors provided (review-first mode)
    review_first_mode = errors_interactive and not continue_errors
    
    # По умолчанию WARNING для консоли, DEBUG в файл
    # Visible mode показывает INFO
    log_level = "DEBUG" if ctx.obj.get('debug', False) else ("INFO" if visible else "WARNING")
    
    # Логи хранятся в папке bender (внутри пакета bender)
    from pathlib import Path
    from backend.services import bender
    bender_pkg_dir = Path(bender.__file__).parent
    log_dir = bender_pkg_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    
    from datetime import datetime
    log_file = f"bender_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    setup_logging(level=log_level, log_dir=str(log_dir), log_file=log_file, file_level="DEBUG")
    
    # Determine worker type (None = auto-select)
    if codex:
        worker_type = 'codex'
    elif opus:
        worker_type = 'opus'
    elif droid:
        worker_type = 'droid'
    else:
        worker_type = None  # Auto-select
    
    # droid_mode теперь count: 0=off, 1=exec only, 2+=droid-droid
    use_droid_exec = droid_mode >= 1
    use_droid_review = droid_mode >= 2
    
    # КРИТИЧНО: Cleanup orphaned процессов от предыдущих запусков
    # Это предотвращает PTY exhaustion (pty_posix_spawn failed)
    if _should_run_global_cleanup():
        try:
            from bender.workers.copilot import cleanup_orphaned_processes
            cleanup_result = cleanup_orphaned_processes()
            if cleanup_result.get('total_killed', 0) > 0 or cleanup_result.get('total_closed', 0) > 0:
                click.echo(f"🧹 Cleaned up {cleanup_result.get('total_killed', 0)} orphaned processes, {cleanup_result.get('total_closed', 0)} windows")
        except Exception:
            pass
    else:
        click.echo("ℹ️  Skipping global cleanup (parallel Bender session detected)")
    
    click.echo(f"🤖 Bender starting...")
    if review_loop:
        if use_droid_review:
            reviewer = "droid"
            executor = "droid"
            click.echo(f"   Mode: DROID-DROID LOOP 🚀 (fastest! max {max_iterations} iterations)")
        elif use_droid_exec:
            executor = "droid"
            reviewer = "copilot" if copilot_review else "codex"
            click.echo(f"   Mode: REVIEW LOOP ({executor}→{reviewer}, max {max_iterations} iterations)")
        else:
            executor = "copilot"
            reviewer = "copilot" if copilot_review else "codex"
            click.echo(f"   Mode: REVIEW LOOP ({executor}→{reviewer}, max {max_iterations} iterations)")
        if continue_errors:
            click.echo(f"   Continue mode: will fix initial errors first")
        elif review_first_mode:
            click.echo(f"   Review-first mode: task assumed done, searching for errors")
    elif worker_type:
        click.echo(f"   Worker: {worker_type} (forced)")
    else:
        click.echo(f"   Worker: auto-select by complexity")
    click.echo(f"   Interval: {interval}s")
    if visible:
        click.echo(f"   Terminal: visible (tmux)")
    if not review_loop:
        click.echo(f"   Mode: {'simple (no verification)' if simple else 'full (with clarification & verification)'}")
    click.echo(f"   Task: {task[:60]}{'...' if len(task) > 60 else ''}")
    click.echo()
    
    # Parse initial errors for continue mode
    initial_errors = None
    if continue_errors:
        # Support both comma-separated and newline/bullet-separated formats
        if '\n' in continue_errors or continue_errors.strip().startswith('-'):
            # Multi-line format: split by newlines first
            import re
            lines = continue_errors.strip().split('\n')
            initial_errors = []
            for line in lines:
                line = line.strip().lstrip('-').strip()
                if not line:
                    continue
                # Only include lines that start with severity markers
                line_upper = line.upper()
                if any(line_upper.startswith(sev) or f": {sev}" in line_upper[:30] 
                       for sev in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']):
                    initial_errors.append(line)
                # Skip BMAD Role Review lines and other non-error lines
        else:
            # Simple comma-separated - also filter by severity
            initial_errors = []
            for e in continue_errors.split(','):
                e = e.strip()
                if not e:
                    continue
                e_upper = e.upper()
                if any(e_upper.startswith(sev) for sev in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']):
                    initial_errors.append(e)
    
    if review_loop:
        # Review loop mode
        asyncio.run(_run_review_loop(task, max_iterations, visible, project, copilot_review, use_droid_exec, use_droid_review, initial_errors, ctx.obj.get('debug', False), review_first_mode, interval, simple, party))
    else:
        asyncio.run(_run_task(task, worker_type, interval, simple, visible, project, ctx.obj.get('debug', False)))


async def _run_review_loop(task: str, max_iterations: int, visible: bool, project_path: Optional[str], use_copilot_reviewer: bool = False, use_droid_exec: bool = False, use_droid_review: bool = False, initial_errors: Optional[list] = None, debug: bool = False, skip_first_execution: bool = False, status_interval: int = 60, skip_llm_analysis: bool = False, use_party_mode: bool = False):
    """Run iterative review loop: worker → reviewer → worker
    
    Args:
        task: The task to perform
        max_iterations: Maximum number of review iterations
        visible: Show terminal windows (tmux)
        project_path: Path to project
        use_copilot_reviewer: Use copilot instead of codex for review
        use_droid_exec: Use droid for execution (-d)
        use_droid_review: Use droid for review too (-dd)
        initial_errors: List of initial errors for continue mode
        debug: Enable debug output
        skip_first_execution: Skip first execution, go straight to review
        status_interval: How often to report status (seconds)
        skip_llm_analysis: Skip GLM analysis (simple mode)
    """
    global _shutdown_event
    
    _shutdown_event = asyncio.Event()
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    try:
        config = load_config()
    except Exception as e:
        click.echo(f"❌ Config error: {e}", err=True)
        sys.exit(1)
    
    from pathlib import Path
    proj_path = Path(project_path) if project_path else Path.cwd()
    
    from bender.llm_router import LLMRouter
    from bender.review import ReviewLoopManager
    from bender.worker_manager import ManagerConfig
    
    # Use multiple API keys if available
    api_keys = config.api_keys_list if config.api_keys_list else None
    gemini_keys = config.gemini_keys_list if config.gemini_keys_list else None
    llm = LLMRouter(config.glm_api_key, requests_per_minute=30, api_keys=api_keys, gemini_api_keys=gemini_keys)
    
    manager_config = ManagerConfig(
        project_path=proj_path,
        check_interval=60.0,
        visible=visible,
        status_interval=float(status_interval),
    )
    
    async def on_status(message: str):
        bender_echo(message)
    
    async def on_ask_user(question: str) -> str:
        click.echo(f"\n❓ {question}")
        response = click.prompt("Your response")
        return response
    
    # Определяем режим
    if use_party_mode:
        click.echo("🎉 Mode: BMAD Party (10-role scoring, threshold 98+)")
    elif use_droid_review:
        click.echo("🚀 Mode: DROID-DROID (fastest! droid for both execution and review)")
    elif use_droid_exec:
        click.echo("🤖 Mode: DROID execution + codex/copilot review")
    
    loop_manager = ReviewLoopManager(
        llm=llm,
        manager_config=manager_config,
        on_status=on_status,
        on_question=on_ask_user,
        use_copilot_reviewer=use_copilot_reviewer,
        skip_llm=skip_llm_analysis,
        use_droid_exec=use_droid_exec,
        use_droid_review=use_droid_review,
        use_party_mode=use_party_mode,
        skip_first_execution=skip_first_execution,
    )
    
    try:
        result = await loop_manager.run_loop(
            task, 
            max_iterations=max_iterations,
            skip_llm_analysis=skip_llm_analysis,
            initial_errors=initial_errors,
        )
        
        click.echo()
        if result.cycle_detected:
            click.echo(f"🔴 Review loop stopped - CYCLE DETECTED!")
            click.echo(f"   Reason: {result.cycle_reason}")
            click.echo(f"   ⚠️  Same errors keep repeating - human intervention needed")
        elif result.success:
            click.echo(f"✅ Review loop completed successfully!")
        else:
            click.echo(f"⚠️  Review loop finished (max iterations reached)")
        
        click.echo(f"   Iterations: {result.iterations}")
        click.echo(f"   Total findings: {result.total_findings}")
        click.echo(f"   Fixed: {result.fixed_findings}")
        
        if result.remaining_findings:
            click.echo(f"\n📝 Remaining findings:")
            for f in result.remaining_findings[:10]:
                click.echo(f"   - {f.severity}: {f.description}")
        
    except asyncio.CancelledError:
        click.echo("\n⚠️  Review loop cancelled")
    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        if debug:
            import traceback
            traceback.print_exc()
    finally:
        # Закрыть терминал и очистить ресурсы
        await loop_manager.cleanup()
        await llm.close()


async def _run_task(task: str, worker_type: Optional[str], interval: int, simple: bool, visible: bool, project_path: Optional[str], debug: bool = False):
    """Async task runner"""
    global _task_manager, _shutdown_event
    
    # Setup shutdown handling
    _shutdown_event = asyncio.Event()
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    try:
        config = load_config()
    except Exception as e:
        click.echo(f"❌ Config error: {e}", err=True)
        click.echo("   Make sure .env file exists with GLM_API_KEY", err=True)
        sys.exit(1)
    
    # Determine project path - use current working directory by default
    if project_path:
        proj_path = Path(project_path)
    else:
        proj_path = Path.cwd()
    
    # Import here to avoid circular imports
    from bender.llm_router import LLMRouter
    from bender.task_manager import TaskManager
    from bender.worker_manager import WorkerType, ManagerConfig
    
    # Create LLM router with rate limiting (30 req/min for Cerebras free tier)
    api_keys = config.api_keys_list if config.api_keys_list else None
    gemini_keys = config.gemini_keys_list if config.gemini_keys_list else None
    llm = LLMRouter(config.glm_api_key, requests_per_minute=30, api_keys=api_keys, gemini_api_keys=gemini_keys)
    
    # Worker type mapping (None = auto-select)
    wt = None
    if worker_type:
        worker_map = {
            'opus': WorkerType.OPUS,
            'droid': WorkerType.DROID,
            'codex': WorkerType.CODEX,
        }
        wt = worker_map.get(worker_type)
    
    # Manager config
    manager_config = ManagerConfig(
        project_path=proj_path,
        check_interval=float(interval),
        visible=visible,
        simple_mode=simple,
    )
    
    # Status callback
    async def on_status(message: str):
        bender_echo(message)
    
    # Human input callback
    async def on_need_human(question: str) -> str:
        click.echo(f"\n❓ {question}")
        response = click.prompt("Your response")
        return response
    
    # Create task manager
    _task_manager = TaskManager(
        glm_client=llm,
        manager_config=manager_config,
        on_status=on_status,
        on_need_human=on_need_human,
    )
    
    try:
        # Run task with auto-select or forced worker
        result = await _task_manager.run_task(
            task, 
            worker_type=wt,  # None = auto-select
            skip_clarification=simple,
        )
        
        # Show result
        click.echo()
        if result.verification_passed:
            click.echo(f"✅ Task completed successfully!")
        else:
            click.echo(f"⚠️  Task finished with issues")
        
        click.echo(f"   Worker: {result.worker_type.value}")
        if result.complexity:
            click.echo(f"   Complexity: {result.complexity.value}")
        click.echo(f"   Attempts: {result.attempts}, Nudges: {result.nudges}")
        click.echo(f"   Time: {result.total_time:.1f}s")
        
        # Show full output from worker (the actual result)
        if result.full_output:
            click.echo()
            click.echo("📄 Result:")
            click.echo("─" * 60)
            # Clean up output - remove ANSI codes and excessive whitespace
            output = result.full_output.strip()
            # Remove common noise patterns
            for noise in ['🤖 Bender visible mode - copilot running...', 'Total usage est:', 'API time spent:', 'Total session time:', 'Total code changes:', 'Breakdown by AI model:']:
                if noise in output:
                    # Keep only the main content before statistics
                    parts = output.split('Total usage est:')
                    if len(parts) > 1:
                        output = parts[0].strip()
                    break
            click.echo(output)
            click.echo("─" * 60)
        
        # Show acceptance criteria if any
        if result.acceptance_criteria and len(result.acceptance_criteria) > 1:
            click.echo()
            click.echo("📝 Acceptance Criteria:")
            for criterion in result.acceptance_criteria[:5]:
                click.echo(f"   ✓ {criterion}")
        
        # Show token usage if available
        if result.input_tokens > 0 or result.output_tokens > 0:
            click.echo()
            click.echo("📊 Token Usage:")
            click.echo(f"   Input:  {result.input_tokens:,}")
            click.echo(f"   Output: {result.output_tokens:,}")
            click.echo(f"   Cached: {result.cached_tokens:,}")
            click.echo(f"   Total:  {result.input_tokens + result.output_tokens:,}")
        
        # Show context stats in debug mode
        if debug:
            ctx_stats = _task_manager.log_watcher.get_context_stats()
            click.echo()
            click.echo("🧠 Context Stats:")
            click.echo(f"   History: {ctx_stats['history_size']} (full: {ctx_stats['full_history_size']})")
            click.echo(f"   Tokens: {ctx_stats['tokens_used']:,} / {ctx_stats['tokens_max']:,} ({ctx_stats['usage_percent']})")
            click.echo(f"   Compressions: {ctx_stats['compressions']}")
        
        # Always show session token usage (GLM supervisor tokens)
        ctx_stats = _task_manager.log_watcher.get_context_stats()
        if ctx_stats['session_total_tokens'] > 0:
            click.echo()
            click.echo("🔮 Bender (GLM) Token Usage:")
            click.echo(f"   Input:  {ctx_stats['session_input_tokens']:,}")
            click.echo(f"   Output: {ctx_stats['session_output_tokens']:,}")
            click.echo(f"   Total:  {ctx_stats['session_total_tokens']:,}")
        
    except asyncio.CancelledError:
        click.echo("\n⚠️  Task cancelled")
    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        if debug:
            import traceback
            traceback.print_exc()
    finally:
        if _task_manager:
            await _task_manager.worker_manager.stop()
        await llm.close()


@cli.command()
@click.pass_context
def status(ctx):
    """Show current Bender status"""
    
    async def _status():
        try:
            config = load_config()
        except Exception as e:
            click.echo(f"❌ Config error: {e}", err=True)
            return
        
        from bender.glm_client import GLMClient
        
        glm = GLMClient(config.glm_api_key)
        
        try:
            # Quick health check
            response = await glm.generate("Say 'ok'", temperature=0)
            click.echo("🤖 Bender Status")
            click.echo(f"   GLM API: ✅ Connected (model: {glm.model_name})")
            click.echo(f"   Project: {config.droid_project_path}")
        except Exception as e:
            click.echo(f"   GLM API: ❌ {e}")
        finally:
            await glm.close()
    
    asyncio.run(_status())


@cli.command()
@click.pass_context  
def attach(ctx):
    """Attach to current worker terminal"""
    import subprocess
    
    # Find bender tmux sessions
    result = subprocess.run(
        ['tmux', 'list-sessions', '-F', '#{session_name}'],
        capture_output=True,
        text=True
    )
    
    sessions = [s for s in result.stdout.strip().split('\n') if s.startswith('bender-')]
    
    if not sessions:
        click.echo("No active Bender sessions found")
        return
    
    if len(sessions) == 1:
        session = sessions[0]
    else:
        click.echo("Active sessions:")
        for i, s in enumerate(sessions):
            click.echo(f"  {i+1}. {s}")
        choice = click.prompt("Select session", type=int, default=1)
        session = sessions[choice - 1]
    
    click.echo(f"Attaching to {session}...")
    subprocess.run(['tmux', 'attach-session', '-t', session])


@cli.command()
@click.pass_context
def cleanup(ctx):
    """Clean up orphaned Bender processes and terminals
    
    Use this if Bender crashed and left processes running,
    or if you see 'PTY not available' errors.
    
    This command:
    - Kills bender-run-*, bender-inner-* scripts
    - Kills script processes that hold PTYs
    - Closes Terminal windows with "BENDER" in title
    - Kills stale tmux sessions
    """
    click.echo("🧹 Cleaning up Bender processes...")
    
    import subprocess
    
    # 1. Cleanup orphaned processes
    try:
        from bender.workers.copilot import cleanup_orphaned_processes
        result = cleanup_orphaned_processes()
        click.echo(f"   Killed processes: {result.get('total_killed', 0)}")
        click.echo(f"   Closed windows: {result.get('total_closed', 0)}")
        if result.get('killed_processes'):
            for p in result['killed_processes'][:5]:
                click.echo(f"      - {p}")
    except Exception as e:
        click.echo(f"   ⚠️ Error cleaning processes: {e}")
    
    # 2. Kill stale tmux sessions
    try:
        from bender.worker_manager import cleanup_stale_bender_sessions
        killed = cleanup_stale_bender_sessions()
        click.echo(f"   Killed tmux sessions: {len(killed)}")
        for s in killed[:5]:
            click.echo(f"      - {s}")
    except Exception as e:
        click.echo(f"   ⚠️ Error cleaning tmux: {e}")
    
    # 3. Force kill any remaining script processes with bender
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            killed_extra = 0
            for line in result.stdout.strip().split('\n'):
                if 'script' in line.lower() and ('bender' in line.lower() or '/tmp/bender' in line):
                    parts = line.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        try:
                            subprocess.run(["kill", "-9", parts[0]], timeout=2)
                            killed_extra += 1
                        except Exception:
                            pass
            if killed_extra:
                click.echo(f"   Extra script processes killed: {killed_extra}")
    except Exception:
        pass
    
    click.echo("✅ Cleanup complete!")


def main():
    """Entry point (legacy — use `lev bender` instead)."""
    import warnings
    warnings.warn(
        "Use "bender" command directly.",
        DeprecationWarning,
        stacklevel=2,
    )
    click.echo(click.style(
        "⚠️  Use "bender" command directly.",
        fg="yellow",
    ))

    # Handle --N shorthand for --interval N
    args = sys.argv[1:]
    new_args = []
    for arg in args:
        if arg.startswith('--') and arg[2:].isdigit():
            new_args.extend(['--interval', arg[2:]])
        else:
            new_args.append(arg)
    sys.argv[1:] = new_args

    cli(obj={})


if __name__ == '__main__':
    main()
