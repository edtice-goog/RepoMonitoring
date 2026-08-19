---
name: fill-triage
description: Fill the monitor's untriaged (yellow) commit events by reasoning verdicts in-chat and seeding the triage cache, then letting the monitor replay them as cache hits — zero Claude API tokens. Use for large initial backfills ("fill triage", "triage the untriaged events"); small incremental updates can just use the monitor's Fill button (Claude API).
---

# Fill untriaged events without API spend

The triage service (`triage-service/claude_server.py`) persists every verdict in
a cache keyed by the request; on a hit it returns the stored verdict with **no
GitHub call and no Claude call**. This skill has YOU produce the verdicts (chat
tokens, much cheaper than API at backfill volume) and write them into that cache;
the monitor's normal fill then consumes them as hits. **No monitor or service
code changes** — someone who prefers the API path just presses Fill without
running this skill first.

## 1. Locate the cache and enumerate the untriaged set

- Verdicts live in **redis** (same instance as the pipeline cache,
  `redis://127.0.0.1:6380/0` unless `REDIS_URL` overrides), keys
  `repomon:triage:<request-key>`. The old `live/triage-cache/` file dir is a
  legacy migration source only — do not write files there.
- `GET http://127.0.0.1:8378/api/results?project=<name>` → events. Untriaged =
  `triage._triage_source` is `cache-only-failsafe` or `error-failsafe` (or no
  `triage` at all) AND `in_scope` is non-empty.
- For each untriaged event collect exactly: `component`, `commit`,
  `files = [m["path"] for m in in_scope]`, and `cross_repo` (verbatim, often absent).
- `vcs_url` = the watch URL for that component:
  `GET /api/watches?project=<name>` → entry `url` (must be the exact string the
  monitor will send, not a normalized variant).

## 2. Reason a verdict per commit

Read the rubric in `SYSTEM_PROMPT` of `triage-service/claude_server.py` and apply
it EXACTLY — same verdicts, same bias (bounds/overflow/UAF guards on in-scope code
→ `response_required`; plausibly-security but unclear → `needs_human_review`;
comments/docs/tests/refactors → `not_meaningful`). Fetch real context per commit:

```bash
gh api repos/<owner>/<repo>/commits/<sha>   # message + per-file patches
```

(or `git -C clones/<owner>/<repo>.git show <sha>` if a local mirror exists).
Judge ONLY the in-scope files' hunks. Do not guess from the commit message alone
when a diff is available. Batch sensibly — dozens per pass is fine.

## 3. Write cache entries the service will recognize

Key and entry shape must match `request_key()` / `cache_put()` in
`claude_server.py` — compute the key with that exact canonicalization:

```python
import hashlib, json
def request_key(vcs_url, commit, files, cross_repo=None):
    cross = sorted([c.get("component"), c.get("path")] for c in (cross_repo or []))
    canon = json.dumps({"vcs_url": vcs_url, "commit": commit,
                        "files": sorted(files), "cross_repo": cross}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()
```

Write each entry to redis as `repomon:triage:<key>` (plain JSON string value):

```python
import redis
r = redis.Redis.from_url("redis://127.0.0.1:6380/0", decode_responses=True)
r.set(f"repomon:triage:{key}", json.dumps(entry))
```

Entry shape:

```json
{
  "request": {"vcs_url": "...", "commit": "...", "files": ["..."]},
  "context_source": "chat-skill",
  "context": {"message": "<commit subject used>", "files": []},
  "model": "chat-skill",
  "usage": {"input_tokens": 0, "output_tokens": 0},
  "created_at": "<utc iso8601>",
  "result": {
    "verdict": "not_meaningful | needs_human_review | response_required",
    "urgency": "critical|high|medium|low or null",
    "vulnerability_class": "<class or null>",
    "reachability_notes": "<or null>",
    "rationale": "<one paragraph tied to the diff>",
    "recommended_action": "<what the vendor should do>",
    "cross_repo_advice": "<only when cross_repo was present, else null>"
  }
}
```

Every `result` field must be present (the monitor renders them verbatim). When
the event carried `cross_repo`, a security-relevant `recommended_action` must say
to propagate the patch to the mirrored locations.

## 4. Verify coverage BEFORE replaying — the fallthrough is silent and paid

`/fill` live-triages every event that misses the cache, server-side, on the
API key in `blackduck.local.json` — the chat session never sees that spend.
So before replaying, recompute `request_key(...)` for EVERY untriaged event
from step 1 and confirm `r.exists(f"repomon:triage:{key}")` for each. The counts must match
exactly (e.g. 475 untriaged -> 475 seeded entries); a shortfall of N means N
silent API calls. Seed the gap first.

## 5. Replay through the monitor

```bash
curl -X POST "http://127.0.0.1:8378/fill?project=<name>&mode=all"
```

Watch the triage service log: every event should print `CACHE HIT`. A `LIVE`
line means a key mismatch (wrong vcs_url string, unsorted files, or missing
cross_repo) — that event just cost API tokens; fix the entry generation before
continuing. Verify on the project page that yellow counts dropped to zero and
spot-check a few verdicts against their commits.
