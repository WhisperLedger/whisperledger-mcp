"""Google OAuth 2.0 helpers for the Jarvis developer portal.

Uses httpx (already in the venv) for token exchange — no extra OAuth library needed.
"""
from __future__ import annotations
import os
import secrets
import urllib.parse
from dataclasses import dataclass

from agent.config import get_env

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@dataclass
class GoogleUserInfo:
    google_id: str
    email: str
    name: str


def get_auth_url(state: str, redirect_uri: str) -> str:
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code(code: str, redirect_uri: str) -> GoogleUserInfo:
    """Exchange an OAuth authorization code for a GoogleUserInfo.

    Raises ValueError if the email domain is not in JARVIS_ALLOWED_GOOGLE_DOMAIN
    or if the address is not verified.
    """
    import httpx

    allowed_domain = get_env("ALLOWED_GOOGLE_DOMAIN", get_env("COMPANY_DOMAIN", "jupiter.money"))

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        user_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_resp.raise_for_status()
        info = user_resp.json()

    email: str = info.get("email", "")
    if not email.endswith(f"@{allowed_domain}"):
        raise ValueError(
            f"Only @{allowed_domain} accounts are allowed. Got: {email!r}"
        )
    if not info.get("verified_email", False):
        raise ValueError("Google account email is not verified.")

    return GoogleUserInfo(
        google_id=str(info["id"]),
        email=email,
        name=info.get("name") or email.split("@")[0],
    )
