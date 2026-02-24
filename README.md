<div align="center">

# 🤖 Bender

### AI Task Supervisor

*He doesn't solve your tasks — he makes sure they get done.*

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

---

**Bender** orchestrates AI CLI tools — [GitHub Copilot CLI](https://docs.github.com/en/copilot), [Codex](https://openai.com/index/codex/), and custom agents — to complete development tasks autonomously. Think of it as a supervisor that delegates work to AI workers, monitors progress, and ensures quality through automated review loops.

[Getting Started](#-getting-started) · [How It Works](#-how-it-works) · [Commands](#-commands) · [Configuration](#-configuration)

</div>

---

## ✨ Features

- 🧠 **Smart Task Routing** — Analyzes task complexity and picks the best AI worker
- 🔄 **Review Loops** — Automatic execute → review → fix cycles until the task is done right
- 🔀 **LLM Failover** — Cerebras (primary) with Gemini fallback, automatic key rotation
- 📊 **Real-time Monitoring** — Watches worker output, detects stalls, nudges when stuck
- 🎯 **Task Clarification** — Breaks down vague tasks into clear acceptance criteria
- 🖥️ **tmux Integration** — Workers run in managed tmux sessions for full visibility
- ⚡ **Multiple Workers** — Copilot, Droid, and Codex adapters with streaming output

## 🏗️ Architecture

```
                        ┌─────────────────┐
                        │   Bender CLI    │
                        │ bender run "…"  │
                        └────────┬────────┘
                                 │
                    ┌────────────▼────────────┐
                    │     Task Manager        │
                    │  ┌──────────────────┐   │
                    │  │ Task Clarifier   │   │
                    │  │ (complexity      │   │
                    │  │  analysis)       │   │
                    │  └──────────────────┘   │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │          Worker Manager              │
              │                                      │
              │  ┌────────┐ ┌───────┐ ┌───────┐     │
              │  │Copilot │ │ Droid │ │ Codex │     │
              │  └────────┘ └───────┘ └───────┘     │
              └──────────────────┬──────────────────┘
                                 │
         ┌───────────────────────▼───────────────────────┐
         │              LLM Router                       │
         │  Cerebras (primary) ──▶ Gemini (fallback)     │
         │  Key rotation · Rate limit handling           │
         └───────────────────────┬───────────────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │  Log Watcher ──▶ Review Loop        │
              │  (monitor)       (verify & fix)     │
              └─────────────────────────────────────┘
```

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- [tmux](https://github.com/tmux/tmux) installed
- At least one AI CLI tool: `copilot`, `droid`, or `codex`
- API key for [Cerebras](https://cloud.cerebras.ai/) and/or [Google Gemini](https://ai.google.dev/)

### Installation

```bash
# Clone the repository
git clone https://github.com/DoroninDobroCorp/bender.git
cd bender

# Install in development mode
pip install -e ".[dev]"

# Set up environment
cp .env.example .env
# Edit .env with your API keys
```

### Quick Start

```bash
# Run a task
bender run "Fix the login bug in auth.py"

# Run with a specific worker
bender run --droid "Add unit tests for utils.py"
bender run --codex "Refactor the database layer"

# Simple mode (skip clarification step)
bender run --simple "Update README"

# Review loop mode
bender review "Implement pagination for the API"
```

## 🎮 Commands

| Command | Description |
|---------|-------------|
| `bender run "task"` | Execute a task with auto-selected worker |
| `bender run --droid "task"` | Force Droid worker for simple tasks |
| `bender run --codex "task"` | Force Codex worker for complex tasks |
| `bender review "task"` | Run task with review loop (execute → review → fix) |
| `bender status` | Show current task status |
| `bender attach` | Attach to the worker's tmux session |
| `bender cleanup` | Clean up orphaned tmux sessions |

### Flags

| Flag | Description |
|------|-------------|
| `--simple` | Skip task clarification, run directly |
| `--visible` | Show worker terminal windows |
| `--interval N` | Log check interval in seconds (default: 60) |
| `--project PATH` | Path to the project directory |
| `--no-push` | Disable auto git push |

## ⚙️ Configuration

Copy `.env.example` to `.env` and configure:

```bash
# Primary LLM: Cerebras
GLM_API_KEY=your-cerebras-key
# Multiple keys for rotation (optional)
GLM_API_KEYS=key1,key2,key3

# Fallback LLM: Google Gemini
GEMINI_API_KEY=your-gemini-key

# Your project path
DROID_PROJECT_PATH=/path/to/your/project

# Display: visible | silent
DISPLAY_MODE=visible
```

### LLM Models

| Provider | Model | Role |
|----------|-------|------|
| Cerebras | `zai-glm-4.7` | Primary — fast inference |
| Google | `gemini-3-flash-preview` | Fallback — high availability |

Bender uses a **smart routing** strategy:
1. Try Cerebras first (fastest)
2. On rate limit (429) → automatic failover to Gemini
3. Key rotation across multiple API keys

## 📁 Project Structure

```
bender/
├── bender/                 # Core library
│   ├── __init__.py         # Public API exports
│   ├── glm_client.py       # Cerebras LLM client
│   ├── gemini_client.py    # Gemini fallback client
│   ├── llm_router.py       # Smart LLM routing & failover
│   ├── task_manager.py     # Task orchestration
│   ├── task_clarifier.py   # Task analysis & complexity scoring
│   ├── worker_manager.py   # Worker lifecycle management
│   ├── log_watcher.py      # Real-time output monitoring
│   ├── log_filter.py       # Output parsing & analysis
│   ├── context_manager.py  # Token budget tracking
│   ├── console_recovery.py # Terminal state recovery
│   ├── review/             # Review loop system
│   │   ├── loop.py         # Review cycle manager
│   │   ├── prompts.py      # LLM review prompts
│   │   └── output_cleaner.py
│   └── workers/            # AI worker adapters
│       ├── base.py         # Abstract worker interface
│       ├── copilot.py      # GitHub Copilot adapter
│       ├── droid.py        # Droid adapter
│       └── codex.py        # OpenAI Codex adapter
├── bender_cli/             # CLI interface
│   ├── main.py             # Click commands
│   └── display.py          # Terminal UI helpers
├── core/                   # Shared config & utilities
│   ├── config.py           # Pydantic settings
│   ├── logging_config.py   # Logging setup
│   └── exceptions.py       # Custom exceptions
├── tests/                  # Test suite
├── scripts/                # Utility scripts
├── .env.example            # Environment template
└── pyproject.toml          # Project configuration
```

## 🔄 How It Works

### 1. Task Clarification
Bender analyzes your task, determines complexity (simple/medium/complex), and generates acceptance criteria.

### 2. Worker Selection
Based on complexity:
- **Simple** → Droid (fast, lightweight)
- **Medium** → Copilot (balanced)
- **Complex** → Codex (powerful, thorough)

### 3. Execution & Monitoring
The selected worker runs in a tmux session. Bender monitors output in real-time, detecting:
- ✅ Task completion
- ⏳ Stalls (nudges the worker)
- ❌ Errors (retries or escalates)

### 4. Review Loop *(optional)*
For critical tasks, Bender runs a review cycle:
```
Execute → LLM Review → Fix Issues → Re-review → ✅ Done
```

## 🛠️ Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy bender/

# Linting
ruff check .

# Format
ruff format .
```

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

*Built with ❤️ by [DoroninDobroCorp](https://github.com/DoroninDobroCorp)*

**Bender doesn't write your code. He makes sure someone does — and does it right.**

</div>
