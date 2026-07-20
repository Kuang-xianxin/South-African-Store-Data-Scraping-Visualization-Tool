"""Exceptions raised while reading the Takealot API."""


class ApiResponseError(RuntimeError):
    """Raised when the API response is invalid or cannot be accepted."""


class AuthenticationError(ApiResponseError):
    """Raised when the configured API key is rejected."""


class RateLimitError(ApiResponseError):
    """Raised when API rate-limit retries have been exhausted."""
