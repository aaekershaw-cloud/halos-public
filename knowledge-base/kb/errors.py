"""Knowledge Base error types"""


class KBError(Exception):
    """Base exception for knowledge base errors"""
    pass


class TransientError(KBError):
    """
    Error that may succeed if retried.

    Examples:
    - Network timeout
    - Rate limit (429)
    - Temporary server error (503)
    - Database lock timeout
    """
    pass


class PermanentError(KBError):
    """
    Error that will not succeed even if retried.

    Examples:
    - Invalid API key (401)
    - Malformed request (400)
    - Context window exceeded (413)
    - LLM returned unparseable output
    - File not found
    - Invalid schema
    """
    pass
