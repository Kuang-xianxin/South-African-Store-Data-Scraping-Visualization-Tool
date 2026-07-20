"""Read-only Takealot Marketplace API access."""

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import ApiResponseError, AuthenticationError, RateLimitError

__all__ = ["ApiResponseError", "AuthenticationError", "RateLimitError", "TakealotClient"]
