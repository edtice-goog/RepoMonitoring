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

## Before you start — two hard preconditions

1. **Model.** Run on the model the service would have used, so a seeded verdict
   is indistinguishable from an API one: `MODEL = "claude-opus-4-6"`
   (`claude_server.py:50`), unless the service was launched with `--model X`.
   Wrong model = verdicts that quietly diverge from the API path. Confirm it
   before seeding anything.
2. **No GitHub API.** Commit context comes from the local mirror clones that
   `fetch_updates` maintains, never `gh api` per commit (§2b). At backfill
   volume the API path is thousands of calls for data already on disk.

Everything downstream is mechanical and must match `claude_server.py` byte for
byte: the payload you reason over (§2c), the cache key (§3), and the entry shape
(§3). Where this skill and the source disagree, **the source wins** — re-read it.

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

### 2a. Run as the SAME model the service would have used

The cached verdict is indistinguishable from an API verdict only if the same
model produced it. `claude_server.py` sets `MODEL = "claude-opus-4-6"` (line 50);
a service started with `--model X` uses X instead — check how it was launched.
**Run this skill in a chat on that model.** Note it in the session before you
start; if you cannot select it, stop and say so rather than seeding verdicts that
silently differ from the API path.

### 2b. Get context from the LOCAL MIRROR, not the GitHub API

`fetch_updates` already maintains a `git clone --mirror --filter=blob:none` of
every watched repo under `$REPOMON_CLONES` (default `<repo-root>/clones`), as
`clones/<owner>/<repo>.git`. Use it. Do NOT call `gh api` per commit — at
backfill volume that is thousands of API calls, and the clone is right there.

```bash
C=clones/<owner>/<repo>.git
git -C $C log -1 --format=%B <sha>                              # commit message
git -C $C diff-tree --no-commit-id --name-only -r <sha>          # changed paths
git -C $C show --format= --unified=3 <sha> -- <in-scope-path>    # patch, one file
```

Message and paths are free (commits+trees are present). Only `show` needs blob
content, which the partial clone lazily fetches over the git protocol — still
not the REST API. If a repo has no clone yet, run
`POST /update?project=<name>&component=<c>` to create it; `gh api` is a last
resort only when the mirror genuinely cannot be produced.

### 2c. Reason over the SAME payload the API path builds

`triage_with_claude()` sends exactly this JSON as the user message, under
`SYSTEM_PROMPT` as system, with `max_tokens=6000`. Reconstruct it per commit and
judge that — nothing more, nothing less:

```json
{
  "component": "<repo name, lowercased>",
  "vcs_url": "<watch url, verbatim>",
  "commit": "<full sha>",
  "commit_message": "<git log -1 --format=%B>",
  "in_scope_files": ["<path>", "..."],
  "diffs": [{"filename": "<path>", "patch": "<hunks>", "truncated": false}],
  "mirrored_in": []
}
```

Match the API path's context rules exactly:

- `diffs` contains **only in-scope files** — the server filters GitHub's file
  list by `set(in_scope)`. Never include an out-of-scope file's diff.
- `patch` is the **hunk text only** (starts at `@@`), as GitHub returns it. Strip
  the `diff --git`/`index`/`---`/`+++` header lines `git show` emits.
- Truncate each patch to **6000 chars** (`PATCH_BUDGET`) and set
  `"truncated": true` when it was longer. Verdicts must not rely on text the API
  path would have cut.
- `mirrored_in` is the event's `cross_repo` verbatim, else `[]`.
- If no diff can be obtained, the server degrades to
  `{"message": None, "files": [{"filename": f} for f in in_scope]}` — reason from
  paths alone and say so in the rationale, exactly as the API path would.

Apply the `SYSTEM_PROMPT` rubric EXACTLY — read it first, it is the scoring
function (`sed -n '53,130p' triage-service/claude_server.py`). Same verdicts, same
bias: bounds/overflow/UAF guards on in-scope code → `response_required`;
plausibly-security but unclear → `needs_human_review`; comments/docs/tests/
refactors → `not_meaningful`. Judge ONLY the in-scope hunks. Do not guess from
the commit message alone when a diff is available. Batch sensibly — dozens per
pass is fine.

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

Only `result` is read on a cache hit (returned with `_triage_source:
"claude-cache"`); `context_source`, `context`, `model` and `usage` are
provenance and are never used as logic. Record them **honestly** anyway — they
are how anyone later tells which verdicts came from this skill, on what model,
and over what evidence. Do not write the placeholder `"model": "chat-skill"`:
put the real model id, so a future re-run can find and redo entries produced by
the wrong one.

```json
{
  "request": {"vcs_url": "...", "commit": "...", "files": ["..."]},
  "context_source": "local-mirror",
  "context": {"message": "<full commit message reasoned over>",
              "files": [{"filename": "<in-scope path>",
                         "patch": "<hunks actually used>",
                         "truncated": false}]},
  "model": "claude-opus-4-6",
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

Every `result` field must be present (the monitor renders them verbatim) and use
exactly the values `TriageResult` allows — `verdict` one of `not_meaningful` /
`needs_human_review` / `response_required`; `urgency` one of `critical` / `high`
/ `medium` / `low` or `null`; the remaining optional fields a string or `null`,
never omitted and never `""`. When the event carried `cross_repo`, a
security-relevant `recommended_action` must say to propagate the patch to the
mirrored locations, and `cross_repo_advice` must be non-null.

Set `context_source` to what you actually had: `"local-mirror"` when you read
diffs from the clone, `"none"` when no diff was obtainable and you reasoned from
paths alone (mirroring the server's own degraded path).

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
