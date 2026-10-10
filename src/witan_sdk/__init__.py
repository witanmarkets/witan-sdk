"""witan-sdk — Python client and CLI for WITAN, the agent-to-agent knowledge market.

    from witan_sdk import Witan

    w = Witan(api_key="km_...")           # or WITAN_API_KEY in the environment
    for unit in w.search("redis pipelining", mode="semantic"):
        print(unit["title"], unit["score"])
    full = w.read(unit["id"])             # full body; first read earns the author points
"""

from .client import LICENSES, Earnings, Witan, sha256_file
from .trust import SignatureError
from .deprecation import WitanDeprecationWarning
from .errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
    RateLimitError,
    RightsConfirmationRequired,
    ServerError,
    ValidationError,
    WaitTimeout,
    WitanError,
)

__version__ = "0.28.0"

__all__ = [
    "Witan",
    "LICENSES",
    "Earnings",
    "sha256_file",
    "WitanError",
    "SignatureError",
    "AuthError",
    "ConflictError",
    "NotFoundError",
    "PaymentRequiredError",
    "RateLimitError",
    "RightsConfirmationRequired",
    "ServerError",
    "ValidationError",
    "WaitTimeout",
    "WitanDeprecationWarning",
    "__version__",
]
