"""Server-side Black Duck READ-credential store.

The monitor reads BoMs from whatever BD SCA server each ingested project points
at (Project.bd_project_url). Clients keep their own PUSH credentials on the
build box (their blackduck.local.json); the monitor must not depend on those.
This is the monitor's own per-server cache — a gitignored JSON file mapping the
server origin to a read token:

    { "https://sca233.poc.blackduck.com": {"api_token": "...", "insecure_tls": false} }

Populated two ways: POST /bd-credential (the Recreate button prompts when a
project's server has no stored key), or automatic migration from a matching
local blackduck.local.json the first time resolve() falls back to it.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE = REPO_ROOT / "bd-credentials.local.json"
LOCAL_CONFIG = REPO_ROOT / "blackduck.local.json"


class MissingBDCredential(Exception):
    """Raised by server-side pipelines when a project's BD server has no stored
    credential. The monitor turns this into a prompt on the Recreate button."""

    def __init__(self, server):
        self.server = server
        super().__init__(f"no stored Black Duck credential for {server} — "
                         f"store one (POST /bd-credential) and retry")


def origin(url) -> str:
    """Normalized server origin for any BD url/link ('' when url is empty)."""
    u = (url or "").strip()
    if not u:
        return ""
    p = urlparse(u if "://" in u else f"https://{u}")
    return f"{(p.scheme or 'https').lower()}://{p.netloc.lower()}"


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def get(server_url):
    """Stored credential dict ({api_token, insecure_tls}) for the server, or None."""
    return _load().get(origin(server_url))


def put(server_url, api_token, insecure_tls=False) -> str:
    key = origin(server_url)
    creds = _load()
    creds[key] = {"api_token": api_token, "insecure_tls": bool(insecure_tls)}
    STORE.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    return key


def resolve(server_url, cfg=None):
    """Credential for the server: the store first; else fall back to a client
    config whose url matches the same origin, MIGRATING it into the store so the
    monitor stops depending on the client file. None when nothing matches."""
    cred = get(server_url)
    if cred:
        return cred
    if cfg is None:
        try:
            cfg = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8-sig"))
        except Exception:
            cfg = {}
    if cfg.get("api_token") and origin(cfg.get("url", "")) == origin(server_url):
        put(server_url, cfg["api_token"], cfg.get("insecure_tls", False))
        return get(server_url)
    return None
