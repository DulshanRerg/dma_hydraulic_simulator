# app/core/auth.py
"""
API-key authentication.

Every protected endpoint declares:
    _ = Depends(require_api_key)

The caller must send:
    X-API-Key: <key>

Keys are read from the API_KEYS environment variable (comma-separated).
"""

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    settings = get_settings()
    if not api_key or api_key not in settings.api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. "
                   "Provide a valid key in the X-API-Key header.",
        )
    return api_key
