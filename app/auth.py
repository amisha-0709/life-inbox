"""Session-based auth + Google OAuth. Never trusts browser-supplied user IDs."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import requests
from fastapi import Cookie, HTTPException, Request, Response

from app.config import load_config
from app.database import (
    ensure_user,
    get_user_by_email,
    get_user_by_google_sub,
    get_user_by_id,
)


SESSION_COOKIE = "life_inbox_session"
OAUTH_STATE_COOKIE = "life_inbox_oauth_state"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
OAUTH_STATE_TTL_SECONDS = 60 * 10
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

logger = logging.getLogger(__name__)


def _signer_key() -> bytes:
    config = load_config()
    return hashlib.sha256(config.session_secret.encode("utf-8")).digest()


def create_session_token(user_id: int) -> str:
    payload = {
        "uid": int(user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(_signer_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse_session_token(token: str) -> Optional[int]:
    try:
        body, sig = token.rsplit(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(
        _signer_key(), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    try:
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("uid")
    expires_at = payload.get("exp")
    if type(user_id) is not int or user_id < 1 or type(expires_at) is not int:
        return None
    if expires_at <= int(time.time()):
        return None
    return user_id


def _secure_cookie() -> bool:
    return load_config().environment in {"beta", "production"}


def set_session_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(user_id),
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def create_oauth_state() -> str:
    # URL/cookie-safe: avoid JSON commas that some cookie jars escape.
    nonce = secrets.token_urlsafe(24)
    exp = int(time.time()) + OAUTH_STATE_TTL_SECONDS
    body = f"{nonce}.{exp}"
    sig = hmac.new(_signer_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse_oauth_state(token: str) -> bool:
    try:
        nonce, exp_text, sig = token.rsplit(".", 2)
    except ValueError:
        return False
    body = f"{nonce}.{exp_text}"
    expected = hmac.new(
        _signer_key(), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        return int(exp_text) >= int(time.time())
    except ValueError:
        return False


def set_oauth_state_cookie(response: Response, state: str) -> None:
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(),
        max_age=OAUTH_STATE_TTL_SECONDS,
        path="/",
    )


def clear_oauth_state_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")


def get_current_user(
    request: Request,
    life_inbox_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    # Never accept X-User-Id / query user_id as identity.
    if "x-user-id" in {k.lower() for k in request.headers.keys()}:
        pass

    token = life_inbox_session
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    user_id = parse_session_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown session user.")
    return user


def optional_current_user(
    request: Request,
    life_inbox_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Optional[dict]:
    try:
        return get_current_user(request, life_inbox_session)
    except HTTPException:
        return None


def dev_login_user(email: str, display_name: Optional[str] = None) -> dict:
    config = load_config()
    if not config.allow_dev_login:
        raise HTTPException(status_code=403, detail="Dev login is disabled.")
    cleaned = email.strip().lower()
    if "@" not in cleaned or len(cleaned) < 5:
        raise HTTPException(status_code=400, detail="Valid email required.")
    existing = get_user_by_email(cleaned)
    if existing:
        return existing
    return ensure_user(
        email=cleaned,
        display_name=display_name or cleaned.split("@", 1)[0],
        auth_provider="dev",
    )


def google_authorize_url(state: str) -> str:
    config = load_config()
    if not config.google_oauth_configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI (or "
                "LIFE_INBOX_PUBLIC_BASE_URL)."
            ),
        )
    params = {
        "client_id": config.google_client_id,
        "redirect_uri": config.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "true",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_google_code(code: str) -> dict:
    """Exchange an authorization code for tokens. Raises HTTPException on failure."""
    config = load_config()
    if not config.google_oauth_configured:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured.")
    try:
        token_response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": config.google_client_id,
                "client_secret": config.google_client_secret,
                "redirect_uri": config.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.exception("Google token exchange network failure")
        raise HTTPException(
            status_code=502,
            detail="Unable to reach Google token endpoint.",
        ) from exc
    if token_response.status_code >= 400:
        logger.warning(
            "Google token exchange failed: %s %s",
            token_response.status_code,
            token_response.text[:300],
        )
        raise HTTPException(
            status_code=400,
            detail="Google authorization code was rejected.",
        )
    try:
        return token_response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Invalid token response from Google.",
        ) from exc


def fetch_google_userinfo(access_token: str) -> dict:
    try:
        response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.exception("Google userinfo network failure")
        raise HTTPException(
            status_code=502,
            detail="Unable to reach Google userinfo endpoint.",
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail="Unable to load Google profile.",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Invalid userinfo response from Google.",
        ) from exc


def login_or_signup_google_profile(profile: dict) -> dict:
    """Create or restore a user from Google OpenID userinfo."""
    subject = str(profile.get("sub") or "").strip()
    email = str(profile.get("email") or "").strip().lower()
    email_verified = profile.get("email_verified")
    if isinstance(email_verified, str):
        email_verified = email_verified.lower() in {"1", "true", "yes"}
    display_name = (
        str(profile.get("name") or "").strip()
        or (email.split("@", 1)[0] if email else "Google user")
    )
    if not subject:
        raise HTTPException(status_code=400, detail="Google profile missing subject.")
    if not email or email_verified is False:
        raise HTTPException(
            status_code=400,
            detail="A verified Google email is required.",
        )

    existing = get_user_by_google_sub(subject)
    if existing:
        return ensure_user(
            email=email,
            display_name=display_name,
            auth_provider="google",
            google_sub=subject,
        )
    return ensure_user(
        email=email,
        display_name=display_name,
        auth_provider="google",
        google_sub=subject,
    )
