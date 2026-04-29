#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "curl_cffi>=0.7",
# ]
# ///
"""
Run this on a machine that can reach claude.ai without Cloudflare interference
(your laptop, not the VPS). It performs Clove's cookie -> OAuth bootstrap and
prints a ready-to-run curl that POSTs the resulting token into your Clove
instance.

Usage:
    uv run scripts/oauth_bootstrap.py

It will prompt for your sessionKey (the cookie value from claude.ai). Nothing
is logged or sent anywhere except claude.ai and console.anthropic.com.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import secrets
import sys
import time
from urllib.parse import urlparse, parse_qs

from curl_cffi import requests

CLAUDE_AI = "https://claude.ai"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_AUTHORIZE_URL_TEMPLATE = "https://claude.ai/v1/oauth/{organization_uuid}/authorize"

BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": CLAUDE_AI,
    "Referer": f"{CLAUDE_AI}/new",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def gen_pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def pick_organization(orgs: list[dict]) -> dict:
    eligible = [o for o in orgs if "chat" in (o.get("capabilities") or [])]
    if not eligible:
        sys.exit("No organization with chat capabilities. Is your account active?")
    eligible.sort(key=lambda o: len(o.get("capabilities") or []), reverse=True)
    return eligible[0]


def main() -> None:
    print(
        "Paste the FULL Cookie header from a working claude.ai browser request.\n"
        "  Quickest way: open https://claude.ai, F12 -> Network, click any request,\n"
        "  copy the value of the `Cookie:` request header (one long line with `;`).\n"
        "  Must include `sessionKey` and Cloudflare's `__cf_bm` / `cf_clearance`\n"
        "  if your IP gets challenged. Pasting only sessionKey will fail with 403.\n",
        file=sys.stderr,
    )
    raw_cookie = getpass.getpass("Paste cookie header (input hidden): ").strip()
    if not raw_cookie:
        sys.exit("Empty cookie, aborting.")
    if "=" not in raw_cookie:
        cookie_header = f"sessionKey={raw_cookie}"
    elif raw_cookie.startswith("sessionKey=") and ";" not in raw_cookie:
        cookie_header = raw_cookie
    else:
        cookie_header = raw_cookie
    if "sessionKey=" not in cookie_header:
        sys.exit("Pasted cookie does not contain sessionKey=, aborting.")

    headers = {**BROWSER_HEADERS, "Cookie": cookie_header}

    print("[1/3] GET /api/organizations ...", file=sys.stderr)
    with requests.Session(impersonate="chrome", timeout=30) as s:
        r = s.get(f"{CLAUDE_AI}/api/organizations", headers=headers, allow_redirects=False)
        if r.status_code != 200:
            sys.exit(
                f"GET organizations failed: HTTP {r.status_code}\n"
                f"first 200 bytes: {r.text[:200]!r}"
            )
        orgs = r.json()
        org = pick_organization(orgs)
        organization_uuid = org["uuid"]
        capabilities = org.get("capabilities") or []
        print(
            f"      org_uuid={organization_uuid} capabilities={capabilities}",
            file=sys.stderr,
        )

        print("[2/3] POST /v1/oauth/<org>/authorize (PKCE) ...", file=sys.stderr)
        verifier, challenge = gen_pkce()
        state = b64url(secrets.token_bytes(32))
        payload = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "organization_uuid": organization_uuid,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": "user:profile user:inference",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        r = s.post(
            OAUTH_AUTHORIZE_URL_TEMPLATE.format(organization_uuid=organization_uuid),
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            allow_redirects=False,
        )
        if r.status_code != 200:
            sys.exit(
                f"authorize failed: HTTP {r.status_code}\n"
                f"first 200 bytes: {r.text[:200]!r}"
            )
        auth_response = r.json()
        redirect_uri = auth_response.get("redirect_uri")
        if not redirect_uri:
            sys.exit(f"No redirect_uri in authorize response: {auth_response!r}")
        qs = parse_qs(urlparse(redirect_uri).query)
        if "code" not in qs:
            sys.exit(f"No code in redirect_uri: {redirect_uri}")
        auth_code = qs["code"][0]
        response_state = qs.get("state", [None])[0]

    print("[3/3] POST console.anthropic.com/v1/oauth/token ...", file=sys.stderr)
    token_data = {
        "code": auth_code,
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": verifier,
    }
    if response_state:
        token_data["state"] = response_state

    # Console rejects browser-fingerprint TLS. Use plain curl_cffi without impersonation.
    with requests.Session(timeout=30) as s:
        r = s.post(
            OAUTH_TOKEN_URL,
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "claude-cli/2.1.81 (external, cli)",
            },
            allow_redirects=False,
        )
        if r.status_code != 200:
            sys.exit(
                f"token exchange failed: HTTP {r.status_code}\n"
                f"first 400 bytes: {r.text[:400]!r}"
            )
        token = r.json()

    expires_at = time.time() + int(token["expires_in"])
    bundle = {
        "organization_uuid": organization_uuid,
        "capabilities": capabilities,
        "oauth_token": {
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": expires_at,
        },
    }

    print("\n=== OAUTH BUNDLE (paste into Clove) ===")
    print(json.dumps(bundle, indent=2))

    print("\n=== Ready-to-run curl ===")
    print(
        "ADMIN_KEY=<your admin key>; CLOVE=<your Clove URL, e.g. http://127.0.0.1:5201>;\\\n"
        f"curl -sS -X POST -H \"x-api-key: $ADMIN_KEY\" -H 'content-type: application/json' \\\n"
        f"  -d '{json.dumps(bundle)}' \\\n"
        f"  $CLOVE/api/admin/accounts"
    )


if __name__ == "__main__":
    main()
