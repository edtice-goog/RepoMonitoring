"""Live Claude triage service for the RepoMonitoring demo.

Drop-in replacement for the stub server.py: same port, same POST /triage
contract ({vcs_url, commit, files}), same response shape — but it calls the
Claude API for a real verdict instead of reading a truth table.

Smart caching (the "if we do the demo again" requirement, and a real efficiency
win for a shared triage service):

    every {vcs_url, commit, files} request is hashed. On a MISS we fetch the
    commit's message + diff from GitHub, ask Claude for a structured verdict,
    and PERSIST input+context+output under live/triage-cache/. On a HIT for the
    same data we return the stored verdict verbatim — no GitHub call, no Claude
    call, no token spend, identical output. Two users (or two demo runs) issuing
    the same request share one answer.

The commit is the unit of triage: all in-scope files are evaluated together and
get one verdict, matching the stub and the design.

Config: anthropic_api_key from blackduck.local.json (gitignored). GitHub context
uses your `gh auth token` (or GITHUB_TOKEN); if unavailable, triage degrades to
message + file-list only and says so.

Usage:
    python triage-service/claude_server.py                 # port 8377 (as the stub)
    python triage-service/claude_server.py --cache-only     # never call out; miss -> failsafe
    python triage-service/claude_server.py --cache-dir DIR --model claude-opus-5
"""

import argparse
import hashlib
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "blackduck.local.json"
DEFAULT_CACHE = REPO_ROOT / "live" / "triage-cache"
# Demo default: a fast, economical model. Triaging illustrative demo commits does
# not warrant frontier-model spend; a production deployment would pass
# --model claude-opus-5 (or newer) where verdict quality justifies the cost.
MODEL = "claude-opus-4-6"
PATCH_BUDGET = 6000  # chars of diff per file sent to Claude (keeps tokens bounded)

SYSTEM_PROMPT = """\
You are a defensive patch-gap triage engine for a vendor that builds embedded \
firmware from open-source components. You are given ONE upstream commit that \
touched files which are compiled into the vendor's build, plus the commit \
message and diff. Decide how urgently the vendor should start a rebuild.

Return exactly one verdict:
- "response_required": the change is security-relevant to this build (e.g. a \
bounds/length check added before an existing copy, an integer-overflow guard, a \
memory-safety fix such as freed-pointer nulling or a use-after-free correction, \
input validation on attacker-reachable data, a fix in a parser/protocol \
handler/crypto path). Silent fixes with no advisory are the important case.
- "needs_human_review": plausibly security-relevant but the diff is ambiguous \
(could be cosmetic, could be a real fix) — route to a person.
- "not_meaningful": no security relevance to this build (refactor, rename, \
formatting, docs, build-system-only, test-only).

Signals to weigh: quiet input-validation/bounds additions; integer-overflow \
guards; memory-safety idioms; sanitizer-style fixes; security-adjacent files \
(parsers, protocol handlers, crypto); commit-message tells OR their conspicuous \
absence; CVE/advisory references. Judge the DIFF, not just the message.

Set `urgency` (critical/high/medium/low) only when response_required, else null. \
`vulnerability_class` and `reachability_notes` are descriptive and optional. \
`recommended_action` is always set.

CROSS-REPO PROPAGATION: if the input includes `mirrored_in` (the same physical \
file is compiled into other repos too — e.g. an inline-vendored copy), then when \
the verdict is security-relevant your `recommended_action` MUST say to propagate \
the fix to those copies, and set `cross_repo_advice` explaining that a fix present \
in one location but not its mirrors means one copy is behind. If not security- \
relevant, leave `cross_repo_advice` null.

SCOPE GUARD: classify and explain urgency ONLY. Never produce exploitation \
guidance, proof-of-concept steps, or any instruction that would help trigger the \
issue. Mirror the neutral language of public CVE/advisory notes."""


# --------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_api_key() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"missing {CONFIG_PATH} (copy blackduck.local.example.json)")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    key = cfg.get("anthropic_api_key", "")
    if not key or "PASTE" in key:
        sys.exit("blackduck.local.json has no real anthropic_api_key")
    return key


def gh_token() -> Optional[str]:
    import os
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        return None


def request_key(req: dict) -> str:
    # Include the cross-repo mirror set: a mirrored-file request yields a
    # patch-propagation verdict that a non-mirrored request must not collide with.
    cross = sorted((c.get("component"), c.get("path"))
                   for c in (req.get("cross_repo") or []))
    canon = json.dumps({
        "vcs_url": req.get("vcs_url"),
        "commit": req.get("commit"),
        "files": sorted(req.get("files", [])),
        "cross_repo": cross,
    }, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def owner_repo(vcs_url: str):
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", (vcs_url or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def fetch_commit_context(vcs_url: str, commit: str, in_scope: list, token: str):
    """Pull the commit message + per-file patches (limited to in-scope files)
    from GitHub. Returns (context_dict, source_str)."""
    owner, repo = owner_repo(vcs_url)
    if not owner or not token:
        return {"message": None, "files": [{"filename": f} for f in in_scope]}, "none"
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "RepoMonitoring-triage",
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"message": None, "files": [{"filename": f} for f in in_scope], "error": str(exc)}, "none"
    scope = set(in_scope)
    files = []
    for f in data.get("files", []):
        if f.get("filename") in scope:
            patch = f.get("patch", "") or ""
            files.append({"filename": f["filename"],
                          "patch": patch[:PATCH_BUDGET],
                          "truncated": len(patch) > PATCH_BUDGET})
    return {"message": data.get("commit", {}).get("message", ""), "files": files}, "github"


# --------------------------------------------------------------- Claude
def triage_with_claude(anthropic_client, vcs_url, commit, in_scope, context, cross_repo=None):
    from pydantic import BaseModel

    class TriageResult(BaseModel):
        verdict: Literal["not_meaningful", "needs_human_review", "response_required"]
        rationale: str
        recommended_action: str
        urgency: Optional[Literal["critical", "high", "medium", "low"]] = None
        vulnerability_class: Optional[str] = None
        reachability_notes: Optional[str] = None
        cross_repo_advice: Optional[str] = None

    owner, repo = owner_repo(vcs_url)
    component = (repo or vcs_url).lower()
    payload = {
        "component": component,
        "vcs_url": vcs_url,
        "commit": commit,
        "commit_message": context.get("message"),
        "in_scope_files": in_scope,
        "diffs": context.get("files", []),
        "mirrored_in": cross_repo or [],
    }
    resp = anthropic_client.messages.parse(
        model=MODEL,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        output_format=TriageResult,
    )
    r = resp.parsed_output
    return {
        "verdict": r.verdict,
        "urgency": r.urgency,
        "vulnerability_class": r.vulnerability_class,
        "reachability_notes": r.reachability_notes,
        "rationale": r.rationale,
        "recommended_action": r.recommended_action,
        "cross_repo_advice": r.cross_repo_advice,
    }, {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


FAILSAFE = {
    "verdict": "needs_human_review",
    "urgency": None,
    "vulnerability_class": None,
    "reachability_notes": None,
    "rationale": "[CACHE-ONLY FAILSAFE] No cached verdict for this request and live "
                 "triage is disabled. Failing safe to human review.",
    "recommended_action": "Route to human review queue.",
}


# --------------------------------------------------------------- HTTP
class ClaudeTriageHandler(BaseHTTPRequestHandler):
    cache_dir: Path = DEFAULT_CACHE
    anthropic_client = None      # None in --cache-only mode
    gh_token_val: Optional[str] = None

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            # Client (monitor) gave up waiting before we finished a slow live
            # triage. The verdict is already cached, so this is harmless.
            print(f"[triage-claude] client disconnected before response (result cached)")

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "triage-claude",
                                  "mode": "cache-only" if self.anthropic_client is None else "live",
                                  "cached": len(list(self.cache_dir.glob("*.json")))})
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/triage":
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "body must be valid JSON"})
            return
        commit = req.get("commit")
        if not commit:
            self._send_json(400, {"error": "missing required field: commit"})
            return

        vcs_url, files = req.get("vcs_url"), req.get("files", [])
        cross = req.get("cross_repo") or []
        key = request_key(req)
        cache_file = self.cache_dir / f"{key}.json"

        # ---- cache hit: return persisted verdict, no external calls ----
        if cache_file.exists():
            entry = json.loads(cache_file.read_text(encoding="utf-8"))
            print(f"[triage-claude] CACHE HIT  {commit[:12]}  verdict={entry['result']['verdict']}")
            self._send_json(200, {"commit": commit, "vcs_url": vcs_url,
                                  "files_evaluated": files, "cross_repo_locations": cross,
                                  **entry["result"], "usage": entry.get("usage"),
                                  "_triage_source": "claude-cache"})
            return

        # ---- cache miss ----
        if self.anthropic_client is None:
            print(f"[triage-claude] MISS (cache-only) {commit[:12]}  -> failsafe")
            self._send_json(200, {"commit": commit, "vcs_url": vcs_url,
                                  "files_evaluated": files,
                                  **FAILSAFE, "_triage_source": "cache-only-failsafe"})
            return

        try:
            context, source = fetch_commit_context(vcs_url, commit, files, self.gh_token_val)
            result, usage = triage_with_claude(self.anthropic_client, vcs_url, commit,
                                               files, context, cross)
        except Exception as exc:  # keep the demo alive on any live-path error
            print(f"[triage-claude] live triage error for {commit[:12]}: {exc}")
            self._send_json(200, {"commit": commit, "vcs_url": vcs_url,
                                  "files_evaluated": files, **FAILSAFE,
                                  "rationale": f"[LIVE ERROR] {exc}. Failing safe to human review.",
                                  "_triage_source": "error-failsafe"})
            return

        entry = {
            "request": {"vcs_url": vcs_url, "commit": commit, "files": files},
            "context_source": source,
            "context": context,
            "model": MODEL,
            "usage": usage,
            "created_at": now_iso(),
            "result": result,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        print(f"[triage-claude] LIVE  {commit[:12]}  verdict={result['verdict']}  "
              f"src={source}  tok={usage['input_tokens']}/{usage['output_tokens']}  -> cached")
        self._send_json(200, {"commit": commit, "vcs_url": vcs_url,
                              "files_evaluated": files, "cross_repo_locations": cross,
                              **result, "usage": usage, "_triage_source": "claude-live"})

    def log_message(self, fmt, *args):
        pass  # quiet; we print our own one-liners


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, ValueError):
            pass

    global MODEL
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--cache-only", action="store_true",
                    help="never call GitHub/Claude; serve cache hits, failsafe on miss")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()
    MODEL = args.model
    ClaudeTriageHandler.cache_dir = args.cache_dir
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    if args.cache_only:
        ClaudeTriageHandler.anthropic_client = None
        mode = "cache-only (no live calls)"
    else:
        try:
            import anthropic
        except ImportError:
            sys.exit("live mode needs the anthropic SDK: pip install -r requirements-live.txt")
        ClaudeTriageHandler.anthropic_client = anthropic.Anthropic(api_key=load_api_key())
        ClaudeTriageHandler.gh_token_val = gh_token()
        mode = f"live ({MODEL}); github={'yes' if ClaudeTriageHandler.gh_token_val else 'no-degrade'}"

    cached = len(list(args.cache_dir.glob("*.json")))
    print(f"[triage-claude] serving on http://127.0.0.1:{args.port}  mode={mode}  "
          f"cache={args.cache_dir} ({cached} entries)")
    HTTPServer(("127.0.0.1", args.port), ClaudeTriageHandler).serve_forever()


if __name__ == "__main__":
    main()
