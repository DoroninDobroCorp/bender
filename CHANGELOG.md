# Changelog

All notable changes to Bender will be documented in this file.

## [2.1.0] - 2026-02-24

### Added
- Review loop system with modular architecture (`bender/review/`)
- Session persistence for crash recovery
- Droid stream formatting utilities
- Comprehensive test suite for workers (copilot, droid, codex)
- Emergency cleanup script (`scripts/bender_emergency_cleanup.sh`)

### Changed
- Updated LLM models: Cerebras `zai-glm-4.7`, Gemini `gemini-3-flash-preview`
- Improved task manager with Droid retry validation
- Enhanced log watcher with NDJSON support
- Better task clarifier with robust approval parsing

### Fixed
- Worker manager SSH host support for remote execution
- Console recovery after tmux session crashes
- Import path cleanup for standalone deployment

## [2.0.0] - 2026-01-15

### Added
- Initial standalone extraction from levgram
- Smart LLM routing (Cerebras → Gemini failover)
- Worker adapters: Copilot, Droid, Codex
- tmux session management
- Task complexity analysis
- CLI interface with Click
