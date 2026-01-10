"""
Custom exceptions for Parser Maker
"""


class ParserMakerError(Exception):
    """Base exception for all Parser Maker errors"""
    pass


# Droid Controller exceptions
class DroidError(ParserMakerError):
    """Base exception for Droid-related errors"""
    pass


class TmuxError(DroidError):
    """Error during tmux operations"""
    pass


class DroidTimeoutError(DroidError):
    """Droid response timeout"""
    pass


class DroidNotRunningError(DroidError):
    """Droid session is not running"""
    pass


# LLM exceptions
class LLMError(ParserMakerError):
    """Base exception for LLM-related errors"""
    pass


class LLMConnectionError(LLMError):
    """Failed to connect to LLM provider"""
    pass


class LLMRateLimitError(LLMError):
    """Rate limit exceeded"""
    pass


class LLMResponseError(LLMError):
    """Invalid or empty response from LLM"""
    pass


class JSONParseError(LLMError):
    """Failed to parse JSON from LLM response"""
    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


# Pipeline exceptions
class PipelineError(ParserMakerError):
    """Base exception for pipeline errors"""
    pass


class StepError(PipelineError):
    """Error during step execution"""
    pass


class EscalationError(PipelineError):
    """Pipeline requires human intervention"""
    pass


# Git exceptions
class GitError(ParserMakerError):
    """Base exception for git operations"""
    pass


class GitConflictError(GitError):
    """Git merge/rebase conflict"""
    pass


class GitAuthError(GitError):
    """Git authentication failed"""
    pass


# Config exceptions
class ConfigError(ParserMakerError):
    """Configuration error"""
    pass


class MissingConfigError(ConfigError):
    """Required configuration is missing"""
    pass
