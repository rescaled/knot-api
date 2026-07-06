"""Static bearer-token authentication."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .dependencies import get_settings_dep

_bearer_scheme = HTTPBearer(auto_error=False, description="Static API token")


def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    expected = settings.token.get_secret_value().encode()
    provided = credentials.credentials.encode() if credentials else b""
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
