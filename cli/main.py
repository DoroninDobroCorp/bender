"""
CLI Interface - команды для управления Parser Maker

Команды:
- parser-maker run <project> - запуск
- parser-maker resume - продолжить
- parser-maker status - статус
"""

import asyncio
import atexit
import signal
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click

from core.config import Config, load_config
from core.logging_config import setup_logging
from core.exceptions import ConfigError, PipelineError
from pipeline.orchestrator import PipelineOrchestrator, PipelineStatus
from state.persistence import StatePersistence
from state.recovery import RecoveryManager
from cli.display import Display, DisplayMode


# Global orchestrator reference for graceful shutdown
_current_orchestrator: Optional[PipelineOrchestrator] = None
_shutdown_event: Optional[asyncio.Event] = None


def _cleanup_tmux_session():
    """Cleanup tmux session on exit (called by atexit)"""
    import subprocess
    if _current_orchestrator and _current_orchestrator.droid:
        try:
            session_name = _current_orchestrator.droid.session_name
            if session_name:
                subprocess.run(
                    ['tmux', 'kill-session', '-t', session_name],
                    capture_output=True,
                    timeout=5
                )
        except Exception:
            pass


# Register atexit handler for cleanup
atexit.register(_cleanup_tmux_session)


def _validate_url(ctx, param, value: str) -> str:
    """Validate URL format"""
    if not value:
        return value
    try:
        result = urlparse(value)
        if not all([result.scheme, result.netloc]):
            raise click.BadParameter(f"Invalid URL format: {value}")
        if result.scheme not in ('http', 'https'):
            raise click.BadParameter(f"URL must use http or https scheme: {value}")
        return value
    except click.BadParameter:
        raise
    except Exception as e:
        raise click.BadParameter(f"Invalid URL: {e}")


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown"""
    import logging
    logger = logging.getLogger(__name__)
    
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    
    # Set shutdown event if available (for async code)
    if _shutdown_event is not None:
        _shutdown_event.set()
        logger.info(f"Shutdown requested ({sig_name}), cleaning up...")
        return
    
    # Fallback: direct cleanup and exit
    logger.warning(f"Shutdown requested ({sig_name}, no event loop), forcing exit...")
    _cleanup_tmux_session()
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


@click.group()
@click.option('--config', '-c', type=click.Path(exists=True), help='Path to .env config file')
@click.option('--verbose', '-v', is_flag=True, help='Verbose output')
@click.pass_context
def cli(ctx, config: Optional[str], verbose: bool):
    """Parser Maker - автоматизация создания парсеров через LLM"""
    ctx.ensure_object(dict)
    
    # Setup logging
    log_level = "DEBUG" if verbose else "INFO"
    setup_logging(level=log_level)
    
    # Загрузить конфигурацию
    try:
        ctx.obj['config'] = load_config(config)
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        ctx.obj['config'] = None
    
    ctx.obj['verbose'] = verbose


@cli.command()
@click.argument('project', type=click.Path(exists=True))
@click.option('--url', '-u', required=True, callback=_validate_url, help='Target URL to parse')
@click.option('--target', '-t', required=True, help='What to parse (e.g., "product prices")')
@click.option('--silent', '-s', is_flag=True, help='Silent mode (only progress)')
@click.option('--no-push', is_flag=True, help='Disable auto git push')
@click.option('--dry-run', is_flag=True, help='Validate config and run health checks without starting pipeline')
@click.pass_context
def run(ctx, project: str, url: str, target: str, silent: bool, no_push: bool, dry_run: bool):
    """Запустить создание парсера для проекта"""
    config: Config = ctx.obj.get('config')
    
    if config is None:
        click.echo("Error: No config loaded. Create .env file or use --config", err=True)
        sys.exit(1)
    
    project_path = Path(project).resolve()
    display_mode = DisplayMode.SILENT if silent else DisplayMode.VISIBLE
    
    # Use config.state_dir or default to project/state
    state_dir = str(project_path / config.state_dir) if config.state_dir != "state" else str(project_path / "state")
    
    display = Display(mode=display_mode)
    display.header(f"Parser Maker - {project_path.name}")
    display.info(f"Target URL: {url}")
    display.info(f"Parse target: {target}")
    if dry_run:
        display.info("Mode: DRY RUN (no changes will be made)")
    display.separator()
    
    async def run_pipeline():
        global _current_orchestrator, _shutdown_event
        
        # Create shutdown event for graceful termination
        _shutdown_event = asyncio.Event()
        
        orchestrator = PipelineOrchestrator(
            project_path=str(project_path),
            gemini_api_key=config.gemini_api_key,
            glm_api_key=config.glm_api_key,
            auto_git_push=not no_push,
            display_mode="silent" if silent else "visible",
            escalate_after=config.bender_escalate_after,
            state_dir=state_dir,
            droid_binary=config.droid_binary
        )
        _current_orchestrator = orchestrator
        
        # Run health check
        display.info("Running pre-flight checks...")
        healthy, issues = await orchestrator.health_check()
        if not healthy:
            for issue in issues:
                display.error(f"  - {issue}")
            display.error("Health check failed. Fix issues and retry.")
            return False
        display.success("All checks passed")
        
        # Dry run mode - exit after health check
        if dry_run:
            display.separator()
            display.success("Dry run completed successfully. Ready to run pipeline.")
            return True
        
        display.separator()
        
        orchestrator.configure(
            target_url=url,
            parse_target=target
        )
        
        # Callbacks
        async def on_step_complete(step_id, state):
            display.step_complete(step_id, state.iteration)
        
        async def on_pipeline_complete(state):
            display.separator()
            display.success(f"Pipeline completed! Total iterations: {state.total_iterations}")
        
        async def on_escalate(reason):
            display.error(f"ESCALATION: {reason}")
            display.info("Human intervention required")
        
        def on_progress(message):
            display.progress(message)
        
        orchestrator.set_callbacks(
            on_step_complete=on_step_complete,
            on_pipeline_complete=on_pipeline_complete,
            on_escalate=on_escalate,
            on_progress=on_progress
        )
        
        try:
            # Create task for pipeline and shutdown monitoring
            pipeline_task = asyncio.create_task(orchestrator.run())
            shutdown_task = asyncio.create_task(_shutdown_event.wait())
            
            done, pending = await asyncio.wait(
                [pipeline_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Check if shutdown was requested
            if shutdown_task in done:
                display.warning("Shutdown requested, cleaning up...")
                return False
            
            state = pipeline_task.result()
            return state.status == PipelineStatus.COMPLETED
        except Exception as e:
            display.error(f"Pipeline failed: {e}")
            return False
        finally:
            _shutdown_event = None
    
    success = asyncio.run(run_pipeline())
    sys.exit(0 if success else 1)


@cli.command()
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path (uses last run if not specified)')
@click.option('--discard-stash', is_flag=True, help='Discard recovery stash and start step fresh')
@click.option('--silent', '-s', is_flag=True, help='Silent mode')
@click.pass_context
def resume(ctx, project: Optional[str], discard_stash: bool, silent: bool):
    """Продолжить прерванный run"""
    config: Config = ctx.obj.get('config')
    
    if config is None:
        click.echo("Error: No config loaded", err=True)
        sys.exit(1)
    
    # Определить project path
    if project:
        project_path = Path(project).resolve()
    else:
        project_path = Path(config.droid_project_path).resolve()
    
    # Use config.state_dir or default
    state_dir = str(project_path / config.state_dir) if config.state_dir != "state" else str(project_path / "state")
    
    display_mode = DisplayMode.SILENT if silent else DisplayMode.VISIBLE
    display = Display(mode=display_mode)
    
    # Проверить recovery
    recovery = RecoveryManager(str(project_path), state_dir)
    info = recovery.check_recovery_needed()
    
    if not info.can_resume:
        display.warning(info.message)
        sys.exit(1)
    
    display.header("Resume Pipeline")
    display.info(info.message)
    
    if info.has_stash:
        if discard_stash:
            display.warning("Discarding recovery stash...")
            recovery.discard_stash()
        else:
            display.info("Applying recovery stash...")
            success, msg = recovery.prepare_recovery(apply_stash=True)
            display.info(msg)
    
    async def run_resume():
        global _current_orchestrator, _shutdown_event
        
        # Create shutdown event for graceful termination
        _shutdown_event = asyncio.Event()
        
        orchestrator = PipelineOrchestrator(
            project_path=str(project_path),
            gemini_api_key=config.gemini_api_key,
            glm_api_key=config.glm_api_key,
            display_mode="silent" if silent else "visible",
            escalate_after=config.bender_escalate_after,
            state_dir=state_dir,
            droid_binary=config.droid_binary
        )
        _current_orchestrator = orchestrator
        
        # Восстановить конфигурацию
        orchestrator.configure(
            target_url=info.state.target_url,
            parse_target=info.state.parse_target
        )
        
        def on_progress(message):
            display.progress(message)
        
        orchestrator.set_callbacks(on_progress=on_progress)
        
        try:
            # Create task for pipeline and shutdown monitoring
            pipeline_task = asyncio.create_task(orchestrator.run_from_step(info.state.current_step))
            shutdown_task = asyncio.create_task(_shutdown_event.wait())
            
            done, pending = await asyncio.wait(
                [pipeline_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Check if shutdown was requested
            if shutdown_task in done:
                display.warning("Shutdown requested, cleaning up...")
                return False
            
            state = pipeline_task.result()
            return state.status == PipelineStatus.COMPLETED
        except Exception as e:
            display.error(f"Resume failed: {e}")
            return False
        finally:
            _shutdown_event = None
    
    success = asyncio.run(run_resume())
    sys.exit(0 if success else 1)


@cli.command()
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path')
@click.pass_context
def status(ctx, project: Optional[str]):
    """Показать статус текущего/последнего run"""
    config: Config = ctx.obj.get('config')
    
    # Определить project path
    if project:
        project_path = Path(project).resolve()
    elif config and config.droid_project_path:
        project_path = Path(config.droid_project_path).resolve()
    else:
        click.echo("Error: No project specified", err=True)
        sys.exit(1)
    
    state_dir = project_path / "state"
    
    display = Display(mode=DisplayMode.VISIBLE)
    display.header(f"Status: {project_path.name}")
    
    persistence = StatePersistence(str(state_dir))
    state = persistence.load()
    
    if state is None:
        display.warning("No runs found")
        sys.exit(0)
    
    # Показать информацию
    display.info(f"Run ID: {state.run_id}")
    display.info(f"Status: {state.status}")
    display.info(f"Current step: {state.current_step}/6")
    display.info(f"Iteration: {state.current_iteration}")
    display.info(f"Confirmations: {state.confirmations}/2")
    display.separator()
    display.info(f"Target URL: {state.target_url}")
    display.info(f"Parse target: {state.parse_target}")
    display.separator()
    display.info(f"Started: {state.started_at}")
    display.info(f"Updated: {state.updated_at}")
    display.info(f"Commits: {len(state.commits)}")
    display.info(f"Iterations logged: {len(state.iterations)}")
    
    if state.has_uncommitted_changes:
        display.warning("Has uncommitted changes!")
    
    if state.recovery_stash:
        display.warning(f"Has recovery stash: {state.recovery_stash}")


@cli.command()
@click.option('--project', '-p', type=click.Path(exists=True), help='Project path')
@click.pass_context
def clear(ctx, project: Optional[str]):
    """Очистить состояние (для нового run)"""
    config: Config = ctx.obj.get('config')
    
    if project:
        project_path = Path(project).resolve()
    elif config and config.droid_project_path:
        project_path = Path(config.droid_project_path).resolve()
    else:
        click.echo("Error: No project specified", err=True)
        sys.exit(1)
    
    state_dir = project_path / "state"
    
    if click.confirm("Clear pipeline state? This cannot be undone."):
        persistence = StatePersistence(str(state_dir))
        persistence.clear()
        click.echo("State cleared.")


def main():
    """Entry point"""
    cli(obj={})


if __name__ == '__main__':
    main()
