"""
Bender CLI - AI Task Supervisor

Bender не решает задачи сам, а следит за их выполнением через CLI инструменты
(copilot, droid, codex).

Команды:
- bender run "задача" - выполнить задачу (default: opus mode)
- bender run --droid "задача" - простая задача через droid
- bender run --codex "задача" - сложная задача через codex
- bender status - текущий статус
- bender attach - присоединиться к терминалу

Параметры:
- --interval N / --N - интервал проверки логов (default: 60s)
- --simple - без перепроверки результата
- --visible - показать терминалы
- --project PATH - путь к проекту
"""

import asyncio
import signal
import sys
from pathlib import Path
from typing import Optional

import click

from core.config import load_config
from core.logging_config import setup_logging


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
    """Handle Ctrl+C"""
    if _task_manager:
        _task_manager.request_stop()
    if _shutdown_event:
        _shutdown_event.set()
    click.echo("\n⚠️  Stopping...")
    # Force exit on second Ctrl+C
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))


@click.group()
@click.option('--debug', is_flag=True, help='Enable debug logging')
@click.pass_context
def cli(ctx, debug):
    """Bender - AI Task Supervisor
    
    Supervises AI tools (copilot, droid, codex) to complete your tasks.
    
    \b
    Examples:
        bender run "Add OAuth authentication"
        bender run --droid "Fix typo in README"
        bender run --codex "Find memory leak in worker.py"
        bender run --interval 10 "Quick fix"
        bender status
        bender attach
    """
    ctx.ensure_object(dict)
    ctx.obj['debug'] = debug


@cli.command()
@click.argument('task', required=False, default=None)
@click.option('--droid', is_flag=True, help='Force droid worker (simple tasks)')
@click.option('--opus', is_flag=True, help='Force opus/copilot worker (medium tasks)')
@click.option('--codex', is_flag=True, help='Force codex worker (complex tasks)')
@click.option('--auto', '-a', is_flag=True, default=True, help='Auto-select worker by complexity (default)')
@click.option('--interval', '-i', type=int, default=60, help='Log check interval in seconds')
@click.option('--simple', '-s', is_flag=True, help='Skip clarification and verification')
@click.option('--visible', '-v', is_flag=True, help='Show terminal windows')
@click.option('--interactive', '-I', is_flag=True, help='Use interactive copilot mode (full terminal, can continue manually)')
@click.option('--review-loop', '-l', is_flag=True, help='Iterative copilot→codex loop until clean')
@click.option('--copilot-review', '-c', is_flag=True, help='Use copilot instead of codex for review (saves codex limits)')
@click.option('--droid-mode', '-d', is_flag=True, help='Use droid (Sonnet) for BOTH execution and review - faster & cheaper!')
@click.option('--max-iterations', type=int, default=10, help='Max iterations for review loop')
@click.option('--continue-errors', '-C', type=str, default=None, help='Continue mode: comma-separated errors to fix first')
@click.option('--errors-interactive', '-E', is_flag=True, help='Enter errors interactively (line by line)')
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path')
@click.pass_context
def run(ctx, task, droid, opus, codex, auto, interval, simple, visible, interactive, review_loop, copilot_review, droid_mode, max_iterations, continue_errors, errors_interactive, project):
    """Run a task with Bender supervision
    
    TASK can be omitted - Bender will ask interactively.
    
    By default, Bender will:
    1. Analyze task complexity
    2. Auto-select worker (droid/opus/codex)
    3. Monitor and nudge if needed
    4. Verify completion
    
    Use --simple to skip analysis and verification.
    Use --droid or --codex to force a specific worker.
    Use --interactive (-I) for full terminal mode (can continue manually if bender stops).
    Use --review-loop for iterative copilot→codex→copilot cycle.
    Use --copilot-review (-c) with --review-loop to use copilot for review.
    Use --droid-mode (-d) to use droid (Sonnet) for BOTH execution and review - faster!
    Use -E to enter errors interactively, or -C "errors" to pass directly.
    
    Examples:
        bender run "Add OAuth authentication"
        bender run -lvI             # Interactive mode with visible terminal
        bender run -lvc              # Loop with copilot
        bender run -lvD              # Loop with droid (faster!)
        bender run -lvcE             # Interactive task + errors
        bender run -lvc -C "bug1, bug2" "task"
    """
    
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
        
        task = "\n".join(lines)
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
            # Join with newline to preserve structure, then clean up
            continue_errors = "\n".join(lines)
        click.echo()
    
    # Track if -E was used but no errors provided (review-first mode)
    review_first_mode = errors_interactive and not continue_errors
    
    # По умолчанию WARNING для консоли, DEBUG в файл
    # Visible mode показывает INFO
    log_level = "DEBUG" if ctx.obj.get('debug', False) else ("INFO" if visible else "WARNING")
    
    # Определяем директорию для логов
    from pathlib import Path
    log_dir = Path.cwd() / "logs"
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
    
    click.echo(f"🤖 Bender starting...")
    if review_loop:
        reviewer = "copilot" if copilot_review else "codex"
        mode_str = "INTERACTIVE" if interactive else "REVIEW LOOP"
        click.echo(f"   Mode: {mode_str} (copilot→{reviewer}→copilot, max {max_iterations} iterations)")
        if interactive:
            click.echo(f"   ⚡ Interactive mode: full terminal, can continue manually")
        if continue_errors:
            click.echo(f"   Continue mode: will fix initial errors first")
        elif review_first_mode:
            click.echo(f"   Review-first mode: task assumed done, searching for errors")
    elif interactive:
        click.echo(f"   Mode: INTERACTIVE (native terminal)")
        click.echo(f"   ⚡ Terminal closes after completion")
    elif worker_type:
        click.echo(f"   Worker: {worker_type} (forced)")
    else:
        click.echo(f"   Worker: auto-select by complexity")
    click.echo(f"   Interval: {interval}s")
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
        # Review loop mode (with or without interactive)
        asyncio.run(_run_review_loop(task, max_iterations, visible, project, copilot_review, droid_mode, initial_errors, ctx.obj.get('debug', False), review_first_mode, interactive, interval, simple))
    elif interactive:
        # Interactive mode WITHOUT review loop - just run task in native terminal
        asyncio.run(_run_interactive_simple(task, visible, project, simple, ctx.obj.get('debug', False), interval))
    else:
        asyncio.run(_run_task(task, worker_type, interval, simple, visible, project, ctx.obj.get('debug', False)))


async def _run_review_loop(task: str, max_iterations: int, visible: bool, project_path: Optional[str], use_copilot_reviewer: bool = False, use_droid_mode: bool = False, initial_errors: Optional[list] = None, debug: bool = False, skip_first_execution: bool = False, use_interactive: bool = False, status_interval: int = 60, skip_llm_analysis: bool = False):
    """Run iterative review loop: worker → reviewer → worker
    
    Args:
        task: The task to perform
        max_iterations: Maximum number of review iterations
        visible: Show terminal windows
        project_path: Path to project
        use_copilot_reviewer: Use copilot instead of codex for review
        use_droid_mode: Use droid (Sonnet) for BOTH execution and review
        initial_errors: List of initial errors for continue mode
        debug: Enable debug output
        skip_first_execution: Skip first execution, go straight to review
        use_interactive: Use interactive copilot mode (full terminal)
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
    from bender.review_loop import ReviewLoopManager
    from bender.worker_manager import ManagerConfig
    
    # Use multiple API keys if available
    api_keys = config.api_keys_list if config.api_keys_list else None
    llm = LLMRouter(config.glm_api_key, requests_per_minute=30, api_keys=api_keys)
    
    manager_config = ManagerConfig(
        project_path=proj_path,
        check_interval=60.0,
        visible=visible,
        interactive_mode=use_interactive,
        status_interval=float(status_interval),
    )
    
    async def on_status(message: str):
        bender_echo(message)
    
    async def on_ask_user(question: str) -> str:
        click.echo(f"\n❓ {question}")
        response = click.prompt("Your response")
        return response
    
    # Определяем режим: droid или copilot
    if use_droid_mode:
        click.echo("🤖 Mode: DROID (Sonnet) for both execution and review")
    elif use_interactive:
        click.echo("🤖 Mode: INTERACTIVE COPILOT (full terminal)")
    
    loop_manager = ReviewLoopManager(
        llm=llm,
        manager_config=manager_config,
        on_status=on_status,
        on_question=on_ask_user,
        use_copilot_reviewer=use_copilot_reviewer,
        use_interactive=use_interactive,
        skip_llm=skip_llm_analysis,
        use_droid_mode=use_droid_mode,
        skip_first_execution=skip_first_execution,
    )
    
    try:
        result = await loop_manager.run_loop(
            task, 
            max_iterations=max_iterations,
            skip_llm_analysis=skip_llm_analysis,
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
        await llm.close()


async def _run_interactive_simple(task: str, visible: bool, project_path: Optional[str], simple: bool, debug: bool = False, status_interval: int = 60):
    """Run task in interactive native terminal mode (no review loop)
    
    This opens a native Terminal.app window with copilot, runs the task,
    and closes the terminal when done.
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
    
    from bender.workers.interactive_copilot import InteractiveCopilotWorker
    from bender.workers.base import WorkerConfig, WorkerStatus
    from bender.task_clarifier import TaskClarifier
    from bender.llm_router import LLMRouter
    
    # Create LLM for task clarification
    api_keys = config.api_keys_list if config.api_keys_list else None
    llm = LLMRouter(config.glm_api_key, requests_per_minute=30, api_keys=api_keys)
    
    async def on_status(message: str):
        bender_echo(message)
    
    click.echo("🤖 Mode: INTERACTIVE COPILOT (native terminal)")
    
    # Clarify task if not simple mode
    clarified_task = task
    if not simple:
        clarifier = TaskClarifier(llm, str(proj_path))
        bender_echo("Analyzing task...")
        result = await clarifier.analyze(task)
        if result:
            bender_echo(f"Complexity: {result.complexity.value}")
            bender_echo(f"Criteria: {len(result.acceptance_criteria)} items")
            # Format task with criteria
            criteria_text = "\n".join([f"- {c}" for c in result.acceptance_criteria])
            clarified_task = f"""{result.clarified_task}

Критерии приёмки:
{criteria_text}

Выполни ВСЕ пункты. После завершения проверь что каждый критерий выполнен."""
    
    # Create worker config
    worker_config = WorkerConfig(
        project_path=proj_path,
        check_interval=float(status_interval),
        visible=True,  # Always visible for interactive
        simple_mode=simple,
    )
    
    # Create interactive worker
    worker = InteractiveCopilotWorker(
        config=worker_config,
        on_status=on_status,
        auto_allow_tools=True,
        status_interval=float(status_interval),
    )
    
    try:
        # Start the worker
        await worker.start(clarified_task)
        bender_echo("Task sent to copilot in native terminal")
        
        # Wait for completion
        success, output = await worker.wait_for_completion(timeout=1800)
        
        if success:
            bender_echo("✅ Task completed successfully!")
        else:
            bender_echo("⚠️ Task may not have completed (timeout or error)")
        
    except asyncio.CancelledError:
        click.echo("\n⚠️  Cancelled")
    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        if debug:
            import traceback
            traceback.print_exc()
    finally:
        # Stop and close terminal
        await worker.stop()
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
    llm = LLMRouter(config.glm_api_key, requests_per_minute=30)
    
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


def main():
    """Entry point"""
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
