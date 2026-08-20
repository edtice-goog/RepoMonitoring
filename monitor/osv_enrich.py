"""OSV.dev cross-check enrichment for the monitor.

An event's commit is checked against the OSV.dev vulnerability database to
answer ONE question with public ground truth: "is this commit a published fix
for a known CVE?" That verdict is independent of the Claude triage, so it acts
as a check on it — a commit OSV lists as a CVE fix that triage called
not_meaningful is a triage miss; the same commit called response_required is
corroboration. No LLM is involved anywhere in this path: local git for the
parent sha, batched HTTPS queries to api.osv.dev, deterministic CVSS
arithmetic.

Detection method (semantics verified against the live API): OSV's /v1/query
{commit} returns the vulnerabilities whose affected GIT range CONTAINS that
commit — so the fix commit itself is NOT returned for the vuln it fixes. We
therefore query the commit's PARENT (still affected) and then look for the
event commit among the returned records' GIT-range "fixed" events. That
exact-sha membership test is authoritative (OSV literally lists fix commits)
and immune to the range-evaluation quirks around incomplete fixes with
multiple "fixed" entries (e.g. zlib CVE-2022-37434).

Politeness contract with api.osv.dev — this is deliberately the bulk shape
OSV asks heavy users to adopt, not a per-commit hammer:
  * parents resolve locally (ONE git rev-list for the whole batch);
  * membership comes from /v1/querybatch, up to 500 commits per request, so a
    full project is a handful of POSTs instead of one per commit;
  * querybatch returns only {id, modified} pairs; full records are fetched
    ONCE per vulnerability id via /v1/vulns/<id>, trimmed to the few fields we
    use, and memoized in redis keyed by id — the `modified` stamp from
    querybatch invalidates the memo exactly when OSV updates the record. The
    old per-commit shape re-downloaded the same full record set (curl: 100+
    records with full references lists) for every commit in the repo;
  * per-commit verdicts are cached in redis (negatives too — "no known CVE"
    is the common case and must not re-query), keys
    repomon:osv:<sha256(url|commit)>, so a re-run makes ZERO requests;
  * failures are never cached (the next run retries), each request gets one
    retry after a short sleep, and batches pace themselves with small sleeps.
"""

import hashlib
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"
BATCH_SIZE = 500                 # querybatch documented max is 1000; stay under
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")
OSV_PREFIX = "repomon:osv:"      # per-commit verdicts
REC_PREFIX = "repomon:osvrec:"   # per-vulnerability trimmed records

_redis = None


class OSVError(Exception):
    """A check failure the caller should surface as a warning."""


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- redis cache
def _backend():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _key(vcs_url, commit):
    return hashlib.sha256(f"{vcs_url}|{commit}".encode()).hexdigest()


def cached(vcs_url, commit):
    """Redis-only lookup (no network, no git) — best-effort: an unreachable
    redis is a miss, never an error, so event rendering can call this freely."""
    try:
        raw = _backend().get(OSV_PREFIX + _key(vcs_url, commit))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _cache_put(vcs_url, commit, entry):
    try:
        _backend().set(OSV_PREFIX + _key(vcs_url, commit), json.dumps(entry))
    except Exception:
        pass  # cache write must never fail the check; next run just re-queries


def _rec_get(rid, modified):
    """Memoized trimmed record, valid while it is at least as new as the
    `modified` stamp querybatch just reported for that id."""
    try:
        raw = _backend().get(REC_PREFIX + rid)
        if raw:
            t = json.loads(raw)
            if not modified or (t.get("modified") or "") >= modified:
                return t
    except Exception:
        pass
    return None


def _rec_put(trec):
    try:
        _backend().set(REC_PREFIX + trec["id"], json.dumps(trec))
    except Exception:
        pass


# --------------------------------------------------------------- CVSS scoring
# OSV severity entries carry a vector string, not a number. Base-score
# arithmetic is fully specified (CVSS v3.1 spec section 7 / v2 guide), so we
# compute it here rather than pulling a dependency. v4 vectors are passed
# through unscored (the MacroVector table lookup isn't worth carrying for a
# badge).
_V3_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _v3_roundup(x):
    # Spec appendix A: ceiling to one decimal, with the float fudge that keeps
    # e.g. 8.6000000004 from rounding up to 8.7.
    i = int(round(x * 100000))
    return (i // 10000) / 10 if i % 10000 == 0 else (i // 10000 + 1) / 10


def _score_v3(vector):
    m = dict(p.split(":", 1) for p in vector.split("/") if ":" in p)
    scope_changed = m.get("S") == "C"
    pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62,
          "H": 0.5 if scope_changed else 0.27}[m["PR"]]
    c, i, a = (_V3_W["CIA"][m[k]] for k in ("C", "I", "A"))
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    impact = (7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15) if scope_changed \
        else 6.42 * iss
    if impact <= 0:
        return 0.0
    expl = 8.22 * _V3_W["AV"][m["AV"]] * _V3_W["AC"][m["AC"]] * pr * _V3_W["UI"][m["UI"]]
    raw = min(1.08 * (impact + expl), 10) if scope_changed else min(impact + expl, 10)
    return _v3_roundup(raw)


_V2_W = {
    "AV": {"L": 0.395, "A": 0.646, "N": 1.0},
    "AC": {"H": 0.35, "M": 0.61, "L": 0.71},
    "Au": {"M": 0.45, "S": 0.56, "N": 0.704},
    "CIA": {"N": 0.0, "P": 0.275, "C": 0.66},
}


def _score_v2(vector):
    m = dict(p.split(":", 1) for p in vector.split("/") if ":" in p)
    c, i, a = (_V2_W["CIA"][m[k]] for k in ("C", "I", "A"))
    impact = 10.41 * (1 - (1 - c) * (1 - i) * (1 - a))
    expl = 20 * _V2_W["AV"][m["AV"]] * _V2_W["AC"][m["AC"]] * _V2_W["Au"][m["Au"]]
    f = 0.0 if impact == 0 else 1.176
    return round(((0.6 * impact) + (0.4 * expl) - 1.5) * f, 1)


def cvss_score(severity_list):
    """(score, vector) from an OSV `severity` array; prefers v3 over v2.
    Returns (None, vector) for vectors we don't score (v4, malformed)."""
    best = (None, None)
    for sev in severity_list or []:
        vec = sev.get("score") or ""
        typ = sev.get("type") or ""
        try:
            if typ == "CVSS_V3":
                return _score_v3(vec), vec
            if typ == "CVSS_V2" and best[0] is None:
                best = (_score_v2(vec), vec)
            elif best[1] is None:
                best = (None, vec)      # v4 etc.: keep the vector, no number
        except (KeyError, ValueError):
            if best[1] is None:
                best = (None, vec)
    return best


def severity_band(score):
    if score is None:
        return None
    if score == 0:
        return "none"
    if score < 4:
        return "low"
    if score < 7:
        return "medium"
    if score < 9:
        return "high"
    return "critical"


# --------------------------------------------------------------- local git
def _batch_parents(clone_dir, shas):
    """{sha: first-parent | None-for-root} for every sha the mirror knows, in
    ONE git call. Shas missing from the result are unknown to the mirror."""
    r = subprocess.run(["git", "-C", str(clone_dir), "rev-list",
                        "--no-walk=unsorted", "--parents", "--ignore-missing",
                        "--stdin"],
                       input="\n".join(shas) + "\n",
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise OSVError(f"git rev-list failed in {clone_dir}: {r.stderr.strip()[:200]}")
    out = {}
    for line in r.stdout.split("\n"):
        parts = line.split()
        if parts:
            out[parts[0]] = parts[1] if len(parts) > 1 else None
    return out


# --------------------------------------------------------------- OSV HTTP
def _http_json(url, body=None):
    """One request with one retry: long batches hit the odd connection reset
    (WinError 10054) that succeeds immediately on the next attempt."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "RepoMonitoring-osv-crosscheck"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            if attempt == 2:
                raise OSVError(f"osv.dev request failed ({url.rsplit('/', 2)[-2]}): {exc}")
            time.sleep(2)


def _query_affecting(sha):
    """Full records affecting `sha` (paginated). Fallback path only — used when
    a querybatch result overflows into pagination."""
    records, page_token, n = [], None, 0
    while True:
        body = {"commit": sha}
        if page_token:
            body["page_token"] = page_token
        data = _http_json(OSV_QUERY_URL, body)
        n += 1
        records.extend(data.get("vulns") or [])
        page_token = data.get("next_page_token")
        if not page_token:
            return records, n


def _display_cve(rec):
    """Prefer the CVE identifier for display; the record id stays the link."""
    rid = rec.get("id") or "?"
    if rid.startswith("CVE-"):
        return rid
    for alias in rec.get("aliases") or []:
        if alias.startswith("CVE-"):
            return alias
    return rid


def _trim(rec):
    """The few fields the monitor uses, small enough to memoize per id."""
    score, vector = cvss_score(rec.get("severity"))
    fixed = {ev["fixed"]
             for aff in rec.get("affected") or []
             for rng in aff.get("ranges") or []
             if rng.get("type") == "GIT"
             for ev in rng.get("events") or [] if ev.get("fixed")}
    return {"id": rec.get("id"), "modified": rec.get("modified") or "",
            "aliases": rec.get("aliases") or [],
            "cvss": score, "band": severity_band(score), "vector": vector,
            "summary": (rec.get("summary") or rec.get("details") or "")[:280],
            "fixed_shas": sorted(fixed)}


def _fix_view(trec):
    """The per-fix dict stored on an event (shape rendered by the monitor)."""
    return {"id": trec["id"], "cve": _display_cve(trec),
            "aliases": trec["aliases"], "cvss": trec["cvss"],
            "severity": trec["band"], "vector": trec["vector"],
            "summary": trec["summary"]}


# --------------------------------------------------------------- public API
def check_batch(vcs_url, commits, clone_dir, force=False):
    """Cross-check many commits of one repo. Returns
    ({sha: (entry, 'cache'|'live')}, http_requests, warnings). Shas the mirror
    doesn't know are reported in warnings and omitted from the result (not
    cached, so a later run after a fetch retries them). Raises OSVError only
    for whole-batch failures (git broke, querybatch unreachable)."""
    out, warnings, nreq = {}, [], 0
    todo = []
    for sha in dict.fromkeys(commits):           # dedupe, keep order
        if not force:
            entry = cached(vcs_url, sha)
            if entry is not None:
                out[sha] = (entry, "cache")
                continue
        todo.append(sha)
    if not todo:
        return out, nreq, warnings

    parents = _batch_parents(clone_dir, todo)
    unknown = [s for s in todo if s not in parents]
    if unknown:
        warnings.append(f"{len(unknown)} commit(s) not in local mirror "
                        f"(e.g. {unknown[0][:12]}); skipped")
    for sha in todo:
        if sha in parents and parents[sha] is None:
            entry = {"fixes": [], "parent": None, "checked_at": _now_iso(),
                     "note": "root commit — no parent to diff against"}
            _cache_put(vcs_url, sha, entry)
            out[sha] = (entry, "live")
    lookup = [(sha, parents[sha]) for sha in todo if parents.get(sha)]

    # Membership per unique parent: ids only, BATCH_SIZE parents per request.
    uniq = list(dict.fromkeys(p for _, p in lookup))
    parent_vulns = {}                            # parent -> [(id, modified)]
    for i in range(0, len(uniq), BATCH_SIZE):
        chunk = uniq[i:i + BATCH_SIZE]
        if i:
            time.sleep(0.5)                      # pace multi-chunk batches
        data = _http_json(OSV_QUERYBATCH_URL,
                          {"queries": [{"commit": p} for p in chunk]})
        nreq += 1
        for p, res in zip(chunk, data.get("results") or []):
            if res.get("next_page_token"):
                # Overflowing result: fall back to the paginated single query,
                # which hands us full records — memoize them on the spot.
                records, n = _query_affecting(p)
                nreq += n
                for rec in records:
                    _rec_put(_trim(rec))
                parent_vulns[p] = [(r.get("id"), r.get("modified") or "")
                                   for r in records]
            else:
                parent_vulns[p] = [(v.get("id"), v.get("modified") or "")
                                   for v in (res.get("vulns") or [])]

    # Full records once per vulnerability id, memo validated by `modified`.
    needed = {}
    for vl in parent_vulns.values():
        for rid, mod in vl:
            if rid and (rid not in needed or mod > needed[rid]):
                needed[rid] = mod
    trimmed = {}
    for rid, mod in needed.items():
        trec = _rec_get(rid, mod)
        if trec is None:
            time.sleep(0.1)                      # pace first-time record pulls
            trec = _trim(_http_json(OSV_VULN_URL + rid))
            nreq += 1
            _rec_put(trec)
        trimmed[rid] = trec

    for sha, parent in lookup:
        fixes = [_fix_view(trimmed[rid])
                 for rid, _ in parent_vulns.get(parent, [])
                 if rid in trimmed and sha in trimmed[rid]["fixed_shas"]]
        fixes.sort(key=lambda f: -(f["cvss"] or 0))
        entry = {"fixes": fixes, "parent": parent, "checked_at": _now_iso()}
        _cache_put(vcs_url, sha, entry)
        out[sha] = (entry, "live")
    return out, nreq, warnings


def check(vcs_url, commit, clone_dir, force=False):
    """Single-commit convenience over check_batch. Returns (entry, source);
    raises OSVError when the commit couldn't be checked."""
    res, _, warnings = check_batch(vcs_url, [commit], clone_dir, force=force)
    if commit not in res:
        raise OSVError(warnings[0] if warnings
                       else f"check failed for {commit[:12]}")
    return res[commit]
