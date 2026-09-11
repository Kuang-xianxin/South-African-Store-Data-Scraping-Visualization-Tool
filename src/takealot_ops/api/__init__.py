"""Read-only Takealot Marketplace API access."""

from takealot_ops.api.client import TakealotClient
from takealot_ops.api.errors import (
    ApiResponseError,
    ApiTransportError,
    AuthenticationError,
    RateLimitError,
)
from takealot_ops.api.authority import install_local_authority

install_local_authority(TakealotClient)

__all__ = [
    "ApiResponseError",
    "ApiTransportError",
    "AuthenticationError",
    "RateLimitError",
    "TakealotClient",
]
