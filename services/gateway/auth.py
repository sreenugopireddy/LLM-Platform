"""
Auth — JWT verification and RBAC.

Token claims we expect:
  sub       : user UUID
  team_id   : billing / routing group
  roles     : list[str]  e.g. ["premium", "admin"]
  exp / iat : standard JWT fields
"""
import os
from dataclasses import dataclass, field
from typing import List
import jwt
from fastapi import HTTPException


@dataclass
class TokenPayload:
    sub: str
    team_id: str
    roles: List[str] = field(default_factory=list)
    exp: int = 0
    iat: int = 0


def verify_token(token: str, secret: str) -> TokenPayload:
    """
    Decode and validate a HS256 JWT.
    Raises HTTP 401 on any failure so gateway returns a uniform error shape.
    """
    if not secret:
        raise HTTPException(500, detail={"error": "jwt_secret_not_configured"})
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp", "team_id"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, detail={"error": "token_expired"})
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, detail={"error": "invalid_token", "detail": str(exc)})

    return TokenPayload(
        sub=payload["sub"],
        team_id=payload["team_id"],
        roles=payload.get("roles", []),
        exp=payload.get("exp", 0),
        iat=payload.get("iat", 0),
    )


def issue_token(sub: str, team_id: str, roles: List[str], secret: str, ttl_seconds: int = 3600) -> str:
    """
    Mint a short-lived token for tests or internal service-to-service calls.
    In production, tokens are issued by your IdP (Azure AD B2C, Auth0, etc.)
    """
    import time
    now = int(time.time())
    payload = {
        "sub": sub,
        "team_id": team_id,
        "roles": roles,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")