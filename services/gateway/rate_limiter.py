"""
Rate Limiter — token-bucket per team, backed by Redis.

Two limits enforced together:
  RPM  (requests per minute)   — burst protection
  TPM  (tokens per minute)     — cost protection

Uses a Lua script executed atomically in Redis to avoid race conditions.
Falls back gracefully if Redis is unavailable (fail-open with a warning log).
"""
import logging
import time
from typing import Tuple, Dict, Optional

logger = logging.getLogger("rate_limiter")

# Lua script: atomic check-and-increment for token-bucket
# Returns [allowed: 0|1, remaining_requests, remaining_tokens]
_LUA_SCRIPT = """
local req_key   = KEYS[1]
local token_key = KEYS[2]
local rpm_limit = tonumber(ARGV[1])
local tpm_limit = tonumber(ARGV[2])
local tokens    = tonumber(ARGV[3])
local window    = 60

local req_count   = tonumber(redis.call('GET', req_key) or '0')
local token_count = tonumber(redis.call('GET', token_key) or '0')

if req_count >= rpm_limit or token_count + tokens > tpm_limit then
  return {0, rpm_limit - req_count, tpm_limit - token_count}
end

local new_req   = redis.call('INCR', req_key)
local new_tok   = redis.call('INCRBY', token_key, tokens)
redis.call('EXPIRE', req_key,   window)
redis.call('EXPIRE', token_key, window)
return {1, rpm_limit - new_req, tpm_limit - new_tok}
"""


class RateLimiter:
    def __init__(self, redis_url: str, default_rpm: int = 60, default_tpm: int = 100_000):
        self.default_rpm = default_rpm
        self.default_tpm = default_tpm
        self._redis = None
        self._script_sha: Optional[str] = None

        if redis_url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(redis_url, decode_responses=True)
                logger.info("Rate limiter connected to Redis at %s", redis_url)
            except ImportError:
                logger.warning("redis package not installed — rate limiting disabled")
        else:
            logger.warning("REDIS_URL not set — rate limiting disabled (fail-open)")

    # Per-team overrides (can be populated from Cosmos DB or env at startup)
    _team_overrides: Dict[str, Dict[str, int]] = {}

    @classmethod
    def set_team_limits(cls, team_id: str, rpm: int, tpm: int) -> None:
        cls._team_overrides[team_id] = {"rpm": rpm, "tpm": tpm}

    async def check(self, team_id: str, estimated_tokens: int = 1000) -> Tuple[bool, dict]:
        """
        Returns (allowed: bool, rate-limit headers: dict).
        Headers follow the standard X-RateLimit-* convention.
        """
        if self._redis is None:
            return True, {}

        limits = self._team_overrides.get(team_id, {})
        rpm = limits.get("rpm", self.default_rpm)
        tpm = limits.get("tpm", self.default_tpm)

        req_key   = f"rl:req:{team_id}"
        token_key = f"rl:tok:{team_id}"

        try:
            if self._script_sha is None:
                self._script_sha = await self._redis.script_load(_LUA_SCRIPT)

            result = await self._redis.evalsha(
                self._script_sha,
                2,
                req_key, token_key,
                rpm, tpm, estimated_tokens,
            )
            allowed, rem_req, rem_tok = bool(int(result[0])), int(result[1]), int(result[2])
            headers = {
                "X-RateLimit-Limit-Requests": str(rpm),
                "X-RateLimit-Limit-Tokens": str(tpm),
                "X-RateLimit-Remaining-Requests": str(max(0, rem_req)),
                "X-RateLimit-Remaining-Tokens": str(max(0, rem_tok)),
                "X-RateLimit-Reset-Requests": str(int(time.time()) + 60),
            }
            if not allowed:
                headers["Retry-After"] = "60"
            return allowed, headers

        except Exception as exc:
            # Fail-open: log and allow the request rather than blocking on Redis failure
            logger.warning("Rate limiter Redis error (fail-open): %s", exc)
            return True, {}