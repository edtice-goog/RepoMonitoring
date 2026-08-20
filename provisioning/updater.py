"""'Check for updates' backfill — LOCAL git, not the GitHub API.

Webhooks handle FUTURE upstream commits. This handles the gap between the version
we built and now, and is the manual fallback when a repo has no webhook. On a busy
branch that gap is thousands of commits (curl master since a release is ~6k), so we
must NOT hit the GitHub REST API per commit. Instead we keep a local **partial mirror
clone** (`--mirror --filter=blob:none`: full commit+tree history, no file blobs) and
read commits + changed files with local `git log --name-status` — one network
transfer, then instant local queries.

Per watched repo we keep a last-seen cursor (Postgres). The walk is a ref range:
first check = `<release tag>..<watch branch>` (exactly the patch gap); later checks =
`<last-seen sha>..<watch branch>`.

    count_pending(...)  -> pending commits per repo (git rev-list --count; instant)
    fetch_updates(...)  -> the chosen commits as webhook payloads for the monitor to
                           fire, plus the cursor advances to persist afterward
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from db.models import EventCursor          # noqa: E402
from db.session import SessionLocal        # noqa: E402
from gh_replay import parse_owner_repo     # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLONES = Path(os.environ.get("REPOMON_CLONES", REPO_ROOT / "clones"))
THRESHOLD = 100          # warn above this many pending commits before processing
MAX_BACKFILL = 2000      # hard ceiling for a single "all" backfill
_RS, _US = "\x1e", "\x1f"   # record / field separators for the log format


# git emits UTF-8 (commit messages, author names, paths). `text=True` alone
# decodes with the LOCALE encoding, which on Windows is cp1252 — a single
# non-ASCII commit message then kills subprocess's reader thread with
# UnicodeDecodeError and takes the whole update with it. Always be explicit;
# errors="replace" keeps a genuinely mis-encoded historical commit from
# aborting a multi-thousand-commit backfill.
_TEXT = {"encoding": "utf-8", "errors": "replace"}


def _git(cwd, *args, check=True, timeout=600):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=check, timeout=timeout, **_TEXT)


# --------------------------------------------------------------- local clone cache
def ensure_clone(url):
    """A partial mirror clone (commits+trees, no blobs). First call clones; later calls
    fetch the latest from the remote. Returns (clone_dir, fetched_ok); fetched_ok is
    False when the refresh fetch failed, so callers can warn the result may be stale
    rather than silently trusting an out-of-date clone."""
    owner, repo = parse_owner_repo(url)
    dest = CLONES / owner / f"{repo}.git"
    fetched = True
    if (dest / "HEAD").exists():
        r = subprocess.run(["git", "-C", str(dest), "remote", "update", "--prune"],
                           capture_output=True, text=True, timeout=600, **_TEXT)
        fetched = (r.returncode == 0)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--mirror", "--filter=blob:none", url, str(dest)],
                       capture_output=True, text=True, check=True, timeout=1800, **_TEXT)
    return dest, fetched


def _resolve_tag(clone, version):
    if not version:
        return None
    v = version.strip()
    u = v.replace(".", "_")
    for cand in (v, f"v{v}", f"OpenSSL_{u}", f"openssl-{v}", f"curl-{u}", f"R_{u}", u):
        r = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                            f"refs/tags/{cand}"], capture_output=True, text=True, **_TEXT)
        if r.returncode == 0:
            return cand
    # Tagless fork (e.g. busybox-w32 monitored on master): the pinned_ref is the
    # build commit itself, not a release tag. Fall back to resolving it as a
    # commit-ish so the first patch-gap walk still has an exclusive lower bound.
    r = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                        f"{v}^{{commit}}"], capture_output=True, text=True, **_TEXT)
    if r.returncode == 0:
        return v
    return None


def _count(clone, base, branch):
    r = _git(clone, "rev-list", "--count", f"{base}..{branch}", check=False)
    return int(r.stdout.strip() or 0) if r.returncode == 0 else 0


def _log(clone, base, branch):
    """Every commit in base..branch (oldest-first) with its changed files, from one
    local `git log --name-status`. No blob fetch (name-status is a tree diff)."""
    fmt = f"{_RS}%H{_US}%cI{_US}%s"
    r = _git(clone, "log", "--reverse", "--name-status", f"--pretty=format:{fmt}",
             f"{base}..{branch}", check=False)
    if r.returncode != 0:
        return []
    commits = []
    for chunk in r.stdout.split(_RS):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        sha, date, subject = lines[0].split(_US)
        added, modified, removed = [], [], []
        for ln in lines[1:]:
            if not ln.strip():
                continue
            parts = ln.split("\t")
            st, path = parts[0], parts[-1]
            (added if st.startswith("A") else removed if st.startswith("D") else modified).append(path)
        commits.append({"sha": sha, "date": date, "subject": subject,
                        "added": added, "modified": modified, "removed": removed})
    return commits


# --------------------------------------------------------------- cursor (Postgres)
def _cursor_get(project_name, url):
    with SessionLocal() as s:
        c = s.query(EventCursor).filter_by(project_name=project_name, repo_url=url).one_or_none()
        return (c.last_seen_at, c.last_seen_sha) if c else (None, None)


def _cursor_set(project_name, url, at, sha):
    with SessionLocal() as s:
        c = s.query(EventCursor).filter_by(project_name=project_name, repo_url=url).one_or_none()
        if c is None:
            c = EventCursor(project_name=project_name, repo_url=url)
            s.add(c)
        c.last_seen_at, c.last_seen_sha = at, sha
        c.updated_at = datetime.now(timezone.utc)
        s.commit()


def commit_advances(project_name, advances):
    for url, at, sha in advances:
        _cursor_set(project_name, url, at, sha)


# --------------------------------------------------------------- planning
def _repos(watches):
    seen, out = set(), []
    for w in watches:
        owner, repo = parse_owner_repo(w.get("url", ""))
        if not owner or not w.get("watch_ref") or (owner, repo) in seen:
            continue
        seen.add((owner, repo))
        out.append({"component": w["component"], "owner": owner, "repo": repo,
                    "branch": w["watch_ref"], "version": w.get("pinned_ref"), "url": w["url"]})
    return out


def _base_for(project_name, clone, r):
    """The exclusive lower bound of the walk: the last-seen sha if we have one, else
    the built release tag (so the first check is exactly the patch gap)."""
    _, last_sha = _cursor_get(project_name, r["url"])
    if last_sha:
        return last_sha, last_sha
    tag = _resolve_tag(clone, r["version"])
    return tag, None


def count_pending(project_name, watches):
    per, total, warnings = [], 0, []
    for r in _repos(watches):
        try:
            clone, fetched = ensure_clone(r["url"])
            if not fetched:
                warnings.append(f"{r['component']}: clone refresh failed; count may be stale")
            base, _ = _base_for(project_name, clone, r)
            if base is None:
                warnings.append(f"{r['component']}: could not resolve base ref for {r['version']}")
                n = 0
            else:
                n = _count(clone, base, r["branch"])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"{r['component']}: {exc}")
            base, n = None, 0
        per.append({"component": r["component"], "repo": f"{r['owner']}/{r['repo']}",
                    "branch": r["branch"], "base": base, "pending": n})
        total += n
    return {"total": total, "threshold": THRESHOLD, "repos": per, "warnings": warnings}


def fetch_updates(project_name, watches, mode, limit=None):
    """mode 'all' = every commit since base (capped at MAX_BACKFILL); 'latest' = the
    newest `limit`. Returns commit EVENTS (oldest-first, same shape the events file /
    driver use) + the cursor advances to persist after firing."""
    events, advances, processed, warnings = [], [], 0, []
    for r in _repos(watches):
        try:
            clone, fetched = ensure_clone(r["url"])
            if not fetched:
                warnings.append(f"{r['component']}: clone refresh failed; may miss new commits")
            base, last_sha = _base_for(project_name, clone, r)
            if base is None:
                # No cursor and no resolvable release ref (claude-inferred
                # version like '?'). The honest semantic: watch FORWARD from
                # the current branch tip - initialize the cursor there so the
                # next check walks real new commits. No historical backfill
                # (there is no anchor to backfill from).
                tip = _git(clone, "rev-parse", r["branch"], check=False).stdout.strip()
                if not tip:
                    warnings.append(f"{r['component']}: could not resolve base ref "
                                    f"or branch {r['branch']!r}")
                    continue
                tip_date = _git(clone, "show", "-s", "--format=%cI", tip,
                                check=False).stdout.strip()
                advances.append((r["url"], tip_date, tip))
                warnings.append(f"{r['component']}: no resolvable release ref "
                                f"({r['version']!r}); watching from current tip {tip[:12]}")
                continue
            commits = _log(clone, base, r["branch"])   # oldest-first
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"{r['component']}: {exc}")
            continue
        commits = [c for c in commits if c["sha"] != last_sha]
        if not commits:
            continue
        chosen = commits[-limit:] if (mode == "latest" and limit) else commits[:MAX_BACKFILL]
        newest = chosen[-1]
        for c in chosen:                                # oldest-first
            events.append({
                "id": f"{r['component']}-{c['sha'][:8]}",
                "vcs_url": f"https://github.com/{r['owner']}/{r['repo']}",
                "branch": r["branch"], "commit": c["sha"], "message": c["subject"],
                "committed_at": c["date"],
                "files_changed": c["added"] + c["modified"] + c["removed"]})
        advances.append((r["url"], newest["date"], newest["sha"]))
        processed += len(chosen)
    return {"events": events, "advances": advances, "processed": processed, "warnings": warnings}


# --------------------------------------------------------------- integrity audit
def audit_events(project_name, watches, db_by_component):
    """Ground-truth integrity check — the operation replay never was: refresh each
    monitored repo's mirror clone, walk the FULL <release tag>..<watch branch>
    range from git itself (deliberately ignoring cursors), and diff that commit
    set against the DB-rendered event history.

    db_by_component: {component: {sha: committed_at_iso}} — the DB side.

    Per repo, three buckets:
      missing_covered     in the repo range, at/after the oldest DB event for the
                          repo, absent from the DB — real gaps; fix fires these.
      older_than_coverage in the range but before DB coverage began (backfill
                          caps / later ingestion) — informational.
      phantom             in the DB but NOT in the branch's ancestry — rewritten
                          history, force-push, or mis-attributed rows; fix
                          removes these.
    Returns {"report": [...], "missing_events": [event-shaped, oldest-first],
             "phantom": {component: [shas]}, "warnings": [...]}."""
    report, missing_events, phantom, warnings = [], [], {}, []
    for r in _repos(watches):
        db = dict(db_by_component.get(r["component"], {}))
        entry = {"component": r["component"], "branch": r["branch"], "db_events": len(db)}
        try:
            clone, fetched = ensure_clone(r["url"])
            if not fetched:
                warnings.append(f"{r['component']}: clone refresh failed; audit may be stale")
            tag = _resolve_tag(clone, r["version"])
            if tag is None:
                warnings.append(f"{r['component']}: cannot resolve base ref "
                                f"{r['version']!r}; skipped")
                continue
            commits = _log(clone, tag, r["branch"])          # oldest-first ground truth
            ancestry = set(_git(clone, "rev-list", r["branch"], check=False)
                           .stdout.split())                  # full branch ancestry
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"{r['component']}: {exc}")
            continue
        entry.update(base=tag, repo_commits=len(commits))
        # Covered window = [oldest, newest] DB event. Missing commits OUTSIDE it
        # are coverage truncation, not corruption: older = pre-backfill history,
        # newer = the cursor has not walked there yet ("Check for updates" is the
        # remedy, and it advances the cursor properly). Only gaps INSIDE the
        # window are integrity failures that audit-fix fires directly.
        floor = min(db.values()) if db else None
        ceil = max(db.values()) if db else None
        missing_cov, older, newer = [], 0, 0
        for c in commits:
            if c["sha"] in db:
                continue
            if floor is None or c["date"] < floor:
                older += 1
            elif c["date"] > ceil:
                newer += 1
            else:
                missing_cov.append(c)
        ph = sorted(sha for sha in db if sha not in ancestry)
        entry.update(missing_in_covered_range=len(missing_cov),
                     older_than_coverage=older, newer_than_coverage=newer,
                     phantom=len(ph),
                     in_sync=(not missing_cov and not ph))
        if ph:
            phantom[r["component"]] = ph
        for c in missing_cov:                                # event-shaped, like fetch_updates
            missing_events.append({
                "id": f"{r['component']}-{c['sha'][:8]}",
                "vcs_url": f"https://github.com/{r['owner']}/{r['repo']}",
                "branch": r["branch"], "commit": c["sha"], "message": c["subject"],
                "committed_at": c["date"],
                "files_changed": c["added"] + c["modified"] + c["removed"]})
        report.append(entry)
    return {"report": report, "missing_events": missing_events,
            "phantom": phantom, "warnings": warnings}
