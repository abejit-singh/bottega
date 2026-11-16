"""Turn exceptions into messages a human can act on.

Used by both the CLI and the dashboard, so the wording lives in one place.
The checks go from most specific to most general, because the SDK's exception
classes inherit from each other (e.g. RateLimitError is an APIStatusError).
"""

from __future__ import annotations

import anthropic

from bottega.agents.base import AgentError


def explain(exc: BaseException) -> str:
    if isinstance(exc, AgentError):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "Authentication failed: set ANTHROPIC_API_KEY in your .env file."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Your API key is not allowed to use this model or feature."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found: check BOTTEGA_MODEL in your .env file."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Claude API. Wait a minute and try again."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "The Claude API took too long to answer. Try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API. Check your internet connection."
    if isinstance(exc, anthropic.AnthropicError):
        # e.g. no credentials configured at all
        return f"Claude SDK error: {exc}"
    return f"Unexpected error: {exc!r}"


def is_fatal(exc: BaseException) -> bool:
    """Errors that will hit every agent the same way: stop the whole project.

    A wrong API key, a model name that doesn't exist or a request the API
    rejects as malformed will not get better by trying another task.
    """
    return isinstance(exc, (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.BadRequestError,
    ))


def is_transient(exc: BaseException) -> bool:
    """Errors worth another try: the network or the API had a bad moment.

    The SDK already retries these a few times by itself; this is for deciding
    whether to retry the whole task after the SDK has given up.
    """
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return True  # APITimeoutError is a kind of APIConnectionError
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500
