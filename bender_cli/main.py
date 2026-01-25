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
- --interval N / --N - интервал проверки логов (default: 30s)
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


# Глобальные ссылки для graceful shutdown
_task_manager = None
_shutdown_event: Optional[asyncio.Event] = None


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
@click.argument('task')
@click.option('--droid', is_flag=True, help='Force droid worker (simple tasks)')
@click.option('--opus', is_flag=True, help='Force opus/copilot worker (medium tasks)')
@click.option('--codex', is_flag=True, help='Force codex worker (complex tasks)')
@click.option('--auto', '-a', is_flag=True, default=True, help='Auto-select worker by complexity (default)')
@click.option('--interval', '-i', type=int, default=30, help='Log check interval in seconds')
@click.option('--simple', '-s', is_flag=True, help='Skip clarification and verification')
@click.option('--visible', '-v', is_flag=True, help='Show terminal windows')
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path')
@click.pass_context
def run(ctx, task, droid, opus, codex, auto, interval, simple, visible, project):
    """Run a task with Bender supervision
    
    By default, Bender will:
    1. Analyze task complexity
    2. Auto-select worker (droid/opus/codex)
    3. Monitor and nudge if needed
    4. Verify completion
    
    Use --simple to skip analysis and verification.
    Use --droid or --codex to force a specific worker.
    """
    
    # Visible mode = always debug logging
    log_level = "DEBUG" if (ctx.obj.get('debug', False) or visible) else "INFO"
    
    # Определяем директорию для логов
    from pathlib import Path
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(exist_ok=True)
    
    from datetime import datetime
    log_file = f"bender_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    setup_logging(level=log_level, log_dir=str(log_dir), log_file=log_file)
    
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
    if worker_type:
        click.echo(f"   Worker: {worker_type} (forced)")
    else:
        click.echo(f"   Worker: auto-select by complexity")
    click.echo(f"   Interval: {interval}s")
    click.echo(f"   Mode: {'simple (no verification)' if simple else 'full (with clarification & verification)'}")
    click.echo(f"   Task: {task[:60]}{'...' if len(task) > 60 else ''}")
    click.echo()
    
    asyncio.run(_run_task(task, worker_type, interval, simple, visible, project, ctx.obj.get('debug', False)))


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
        click.echo(f"📋 {message}")
    
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
