class PermanentError(Exception):
    """Retrying cannot help (blocked URL, 404, missing API key, missing input...).
    The orchestrator fails the task immediately instead of retrying it."""
