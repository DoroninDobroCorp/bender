# Bender - Architecture

## Overview

Bender is a supervisor for AI CLI tools (GitHub Copilot, Droid, Codex). It doesn't solve tasks itself - it orchestrates workers and ensures quality through review loops.

```
┌─────────────────────────────────────────────────────────────┐
│  User: bender run -lvI "Add OAuth"                          │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  CLI (bender_cli/main.py)                                   │
│  Parse options → Select mode → Start async runner           │
└─────────────────────────────────────────────────────────────┘
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
┌─────────────────────┐         ┌─────────────────────┐
│  TaskManager        │         │  ReviewLoopManager  │
│  (single task)      │         │  (iterative cycle)  │
└─────────────────────┘         └─────────────────────┘
          │                               │
          └───────────────┬───────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  WorkerManager                                              │
│  ├─ CopilotWorker (non-interactive, -p mode)                │
│  ├─ InteractiveCopilotWorker (interactive, tmux session) ⭐ │
│  ├─ DroidWorker                                             │
│  └─ CodexWorker                                             │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  tmux session                                               │
│  ├─ Full scrollback (5000 lines)                            │
│  ├─ Terminal.app window (visible mode)                      │
│  └─ Survives bender crash (can continue manually)           │
└─────────────────────────────────────────────────────────────┘
```

## Key Components

### 1. Workers

| Worker | Mode | Use Case |
|--------|------|----------|
| `CopilotWorker` | Non-interactive (`copilot -p`) | Quick tasks, batch mode |
| `InteractiveCopilotWorker` | Interactive (tmux) | Full terminal, can continue manually |
| `DroidWorker` | tmux | Simple tasks |
| `CodexWorker` | tmux | Complex tasks, code review |

### 2. InteractiveCopilotWorker (New!)

The star of the show. Key features:

```python
class InteractiveCopilotWorker:
    # Runs copilot in interactive mode (no -p flag)
    # Uses tmux with large scrollback
    # Auto-responds to permission prompts
    # Reports status every N seconds
    # Session survives bender crash
```

**How it works:**
1. Creates tmux session with `copilot` (interactive mode)
2. Opens Terminal.app window with `tmux attach`
3. Sends task via `tmux send-keys`
4. Monitors output via `tmux capture-pane`
5. Detects permission prompts → auto-responds 'y'
6. Detects completion markers → marks task complete
7. On stop: keeps session running (user can continue)

### 3. ReviewLoopManager

Iterative cycle: copilot → reviewer → copilot until clean.

```
Iteration 1:
├─ Copilot executes task
├─ Reviewer (codex/copilot) checks code
├─ GLM analyzes findings
└─ Decision: fix/skip/done

If fix:
├─ Copilot fixes issues
└─ Back to reviewer...

Until done or max iterations
```

### 4. LLM Router

GLM (Cerebras) as primary, Qwen as fallback:

```python
class LLMRouter:
    # Primary: GLM (zai-glm-4.7) - thinking model
    # Fallback: Qwen (qwen-3-235b-a22b-instruct-2507)
    # API key rotation for rate limits
    # Auto-retry with exponential backoff
```

## Data Flow

### Standard Mode

```
User → TaskManager → WorkerManager → CopilotWorker → tmux → copilot
                                            ↓
                                      capture output
                                            ↓
                                    LogWatcher → LLM analysis
                                            ↓
                                      nudge if stuck
```

### Interactive Mode

```
User → ReviewLoopManager → WorkerManager → InteractiveCopilotWorker
                                                    ↓
                                              tmux session
                                                    ↓
                                           Terminal.app window
                                                    ↓
                                           User sees everything
                                                    ↓
                               ┌─────────────────────────────────────┐
                               │  Monitor loop (every 2s):           │
                               │  ├─ capture-pane → read output      │
                               │  ├─ detect permissions → auto 'y'   │
                               │  ├─ detect questions → ask human    │
                               │  ├─ detect completion → mark done   │
                               │  └─ status report (every 30s)       │
                               └─────────────────────────────────────┘
```

## File Structure

```
bender/
├── bender/                    # Core logic
│   ├── __init__.py            # Exports
│   ├── glm_client.py          # Cerebras API client
│   ├── llm_router.py          # GLM + Qwen routing
│   ├── worker_manager.py      # Worker lifecycle
│   ├── task_manager.py        # Single task runner
│   ├── task_clarifier.py      # Task analysis + criteria
│   ├── review_loop.py         # Iterative review cycle
│   ├── log_watcher.py         # Output analysis
│   ├── log_filter.py          # Output filtering
│   ├── context_manager.py     # Token budget
│   ├── utils.py               # Helpers
│   └── workers/
│       ├── base.py            # BaseWorker abstract class
│       ├── copilot.py         # Non-interactive copilot
│       ├── interactive_copilot.py  # Interactive copilot ⭐
│       ├── droid.py           # Droid worker
│       └── codex.py           # Codex worker
│
├── bender_cli/                # CLI interface
│   └── main.py                # Click commands
│
├── core/                      # Configuration
│   ├── config.py              # Load .env
│   ├── exceptions.py          # Custom exceptions
│   └── logging_config.py      # Logging setup
│
└── tests/                     # Tests
    ├── test_bender.py
    └── test_core.py
```

## Key Design Decisions

### 1. tmux over subprocess
- Full terminal emulation
- Scrollback history
- Survives parent process crash
- User can attach and continue

### 2. Interactive mode as default for review loop
- One tmux session per loop (not per task)
- Sends next task to same session
- User sees continuous flow

### 3. Minimal LLM calls
- Only for status reports (every 30-60s)
- Only for analyzing findings
- No LLM for permission detection (regex)

### 4. Session persistence
- On `stop()`: keeps session if visible mode
- Logs: `tmux attach -t bender-copilot-interactive-XXXX`
- User can continue manually

## Configuration

```env
# Required
GLM_API_KEY=csk-...

# Optional: multiple keys for rotation
GLM_API_KEYS=csk-key1,csk-key2,csk-key3

# Optional: project path
DROID_PROJECT_PATH=/path/to/project
```
