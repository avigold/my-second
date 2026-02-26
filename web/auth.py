"""OAuth authentication: Lichess (PKCE), Chess.com, and Google."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import redirect, request, session

# ---------------------------------------------------------------------------
# Config — read from environment
# ---------------------------------------------------------------------------

LICHESS_CLIENT_ID      = os.environ.get("LICHESS_CLIENT_ID", "")
CHESSCOM_CLIENT_ID     = os.environ.get("CHESSCOM_CLIENT_ID", "")
CHESSCOM_CLIENT_SECRET = os.environ.get("CHESSCOM_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID       = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET   = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _app_url() -> str:
    return os.environ.get("APP_URL", "https://mysecond.app").rstrip("/")


def _redirect_uri(platform: str) -> str:
    return f"{_app_url()}/auth/{platform}/callback"


# ---------------------------------------------------------------------------
# Lichess — PKCE OAuth 2.0 (no client secret needed)
# ---------------------------------------------------------------------------

def lichess_enabled() -> bool:
    return bool(LICHESS_CLIENT_ID)


def lichess_auth_url() -> str:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state     = secrets.token_urlsafe(16)

    session["pkce_verifier"] = verifier
    session["oauth_state"]   = state

    params = {
        "response_type":         "code",
        "client_id":             LICHESS_CLIENT_ID,
        "redirect_uri":          _redirect_uri("lichess"),
        "code_challenge_method": "S256",
        "code_challenge":        challenge,
        "state":                 state,
    }
    return "https://lichess.org/oauth?" + urlencode(params)


def lichess_handle_callback(registry) -> dict | None:
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.pop("oauth_state", None):
        return None

    verifier = session.pop("pkce_verifier", None)
    if not verifier:
        return None

    resp = requests.post(
        "https://lichess.org/api/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "code_verifier": verifier,
            "redirect_uri":  _redirect_uri("lichess"),
            "client_id":     LICHESS_CLIENT_ID,
        },
        timeout=10,
    )
    if not resp.ok:
        return None

    token = resp.json().get("access_token")
    if not token:
        return None

    account = requests.get(
        "https://lichess.org/api/account",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).json()

    username = account.get("username")
    if not username:
        return None

    return registry.upsert_user(lichess_id=username, username=username)


# ---------------------------------------------------------------------------
# Chess.com — standard OAuth 2.0
# ---------------------------------------------------------------------------

def chesscom_enabled() -> bool:
    return bool(CHESSCOM_CLIENT_ID and CHESSCOM_CLIENT_SECRET)


def chesscom_auth_url() -> str:
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id":     CHESSCOM_CLIENT_ID,
        "redirect_uri":  _redirect_uri("chesscom"),
        "state":         state,
        "scope":         "openid",
    }
    return "https://oauth.chess.com/authorize?" + urlencode(params)


def chesscom_handle_callback(registry) -> dict | None:
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.pop("oauth_state", None):
        return None

    resp = requests.post(
        "https://oauth.chess.com/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     CHESSCOM_CLIENT_ID,
            "client_secret": CHESSCOM_CLIENT_SECRET,
            "redirect_uri":  _redirect_uri("chesscom"),
        },
        timeout=10,
    )
    if not resp.ok:
        return None

    token = resp.json().get("access_token")
    if not token:
        return None

    account = requests.get(
        "https://oauth.chess.com/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).json()

    # Chess.com returns { "username": "...", ... } or { "preferred_username": "..." }
    username = (account.get("username")
                or account.get("preferred_username")
                or account.get("sub", "").split("/")[-1])
    if not username:
        return None

    return registry.upsert_user(chesscom_id=username, username=username)


# ---------------------------------------------------------------------------
# Google — standard OAuth 2.0 + OpenID Connect
# ---------------------------------------------------------------------------

def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_auth_url() -> str:
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  _redirect_uri("google"),
        "scope":         "openid email profile",
        "state":         state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def google_handle_callback(registry) -> dict | None:
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.pop("oauth_state", None):
        return None

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  _redirect_uri("google"),
        },
        timeout=10,
    )
    if not resp.ok:
        return None

    token = resp.json().get("access_token")
    if not token:
        return None

    userinfo = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).json()

    google_id = userinfo.get("sub")
    if not google_id:
        return None

    # Prefer display name; fall back to email prefix
    username = (userinfo.get("name")
                or (userinfo.get("email") or "").split("@")[0]
                or google_id)

    return registry.upsert_user(google_id=google_id, username=username)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_current_user() -> dict | None:
    if "user_id" not in session:
        return None
    return {
        "id":       session["user_id"],
        "username": session.get("username", ""),
        "platform": session.get("platform", ""),
        "role":     session.get("role", "user"),
    }


def set_session_user(user: dict) -> None:
    session["user_id"]  = user["id"]
    session["username"] = user["username"]
    session["platform"] = user.get("platform", "")
    session["role"]     = user.get("role", "user")


def login_required(f):
    """Decorator: redirect to /login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user():
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated
