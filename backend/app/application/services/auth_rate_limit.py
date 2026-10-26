"""Small Redis-backed budgets for public authentication endpoints."""
import hashlib

from fastapi import HTTPException

from app.application.errors.exceptions import AppException


class AuthRateLimiter:
    # All budgets are checked and incremented together, with expiry attached in
    # the same Redis operation. An outage cannot silently disable the limit.
    _RESERVE = """
local retry = 0
for i, key in ipairs(KEYS) do
  local maximum = tonumber(ARGV[(i - 1) * 2 + 1])
  if tonumber(redis.call('GET', key) or '0') >= maximum then
    retry = math.max(retry, redis.call('TTL', key), 1)
  end
end
if retry > 0 then return retry end
for i, key in ipairs(KEYS) do
  local count = redis.call('INCR', key)
  if count == 1 then redis.call('EXPIRE', key, tonumber(ARGV[(i - 1) * 2 + 2])) end
end
return 0
"""

    @staticmethod
    def _key(operation: str, dimension: str, value: str) -> str:
        digest = hashlib.sha256(value.encode()).hexdigest()
        return f"auth:limit:{operation}:{dimension}:{digest}"

    async def check(self, operation: str, email: str, client_address: str) -> None:
        from app.infrastructure.storage.redis import get_redis
        if operation not in {"login", "register"}:
            raise ValueError("Unsupported authentication rate limit")
        normalized_email = email.strip().lower()
        keys = [self._key(operation, "account", normalized_email), self._key(operation, "client", client_address)]
        # The client address is the actual TCP peer, never an untrusted XFF
        # header. A generous shared budget also supports the local reverse proxy.
        budgets = (10, 300, 300, 300) if operation == "login" else (3, 3600, 100, 3600)
        store = get_redis()
        try:
            await store.initialize()
            retry = int(await store.client.eval(self._RESERVE, len(keys), *keys, *budgets))
        except Exception:
            raise AppException(503, "Authentication is temporarily unavailable", 503) from None
        if retry:
            raise HTTPException(status_code=429, detail="Too many authentication attempts. Please try again later.", headers={"Retry-After": str(retry)})

    async def login_succeeded(self, email: str) -> None:
        from app.infrastructure.storage.redis import get_redis
        store = get_redis()
        try:
            await store.initialize()
            await store.client.delete(self._key("login", "account", email.strip().lower()))
        except Exception:
            raise AppException(503, "Authentication is temporarily unavailable", 503) from None


def get_auth_rate_limiter() -> AuthRateLimiter:
    return AuthRateLimiter()
