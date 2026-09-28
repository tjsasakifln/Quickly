"""Application-wide request rate limiting.

Keep the limiter in a dependency-free module so route modules can decorate
security-sensitive endpoints without importing :mod:`app.main` (which would
create an import cycle).
"""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter

from app.client_ip import client_ip_from_request


# Public authentication endpoints deliberately have independent buckets.
# SlowAPI's default key style is URL, so consuming one limit does not consume
# another endpoint's allowance.
AUTH_LOGIN_LIMIT = "10/minute"
AUTH_REFRESH_LIMIT = "30/minute"
AUTH_SETUP_STATUS_LIMIT = "30/minute"
AUTH_FIRST_ADMIN_LIMIT = "5/hour"


def rate_limit_key(request: Request) -> str:
    """Return the best available client address for request throttling."""
    return client_ip_from_request(request) or "unknown"


limiter = Limiter(key_func=rate_limit_key, default_limits=["200/minute"])
