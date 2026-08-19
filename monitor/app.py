"""RepoMonitoring — monitoring component (demo web application).

Multi-project. Each --data-dir is one Black Duck SCA project (1:1). The landing
page lists the projects; drilling into one shows its watch manifest and the
commit events for that project. In production this is fed by the BD SCA
notification API; the demo reads the capture + SBOM artifacts from disk.

    GET  /                     project list (landing)
    GET  /?project=<name>      one project: watch manifest + its commit events
    GET  /health               service health
    GET  /api/projects         project summaries as JSON
    GET  /api/watches?project=  watch manifest as JSON
    GET  /api/results?project=  processed commit results as JSON
    POST /webhook              Git webhook; routed to every project watching the repo

Webhook semantics (per project): the commit hash is the unit of selection and
triage. Changed files are compared against that project's compiled-file index;
a commit with >=1 in-scope file is triaged (all in-scope files together), else
suppressed. A commit on a reference-only repo is not_monitored. An incoming push
is routed to EVERY project whose watch manifest contains the repo.

Usage:
    python app.py [--port 8378] [--data-dir DIR ...]
                  [--triage-url http://127.0.0.1:8377/triage]
"""

import argparse
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # repo root (for provisioning.* / db.*)

# Monitor-local operational state: project -> the exact data dir it was last loaded from,
# so a restart reloads the same dirs (a project can have several stale live-* dirs on disk).
LOADED_MANIFEST = REPO_ROOT / ".monitor-loaded.json"

DEFAULT_TRIAGE_URL = "http://127.0.0.1:8377/triage"


# --------------------------------------------------------------- helpers
def norm_url(url: str) -> str:
    """Normalize a VCS URL for comparison (scheme/case/.git/trailing-slash)."""
    u = url.strip().lower().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    for prefix in ("https://", "http://", "git://", "ssh://git@"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    return u


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def attr(s):
    """esc() for HTML attribute values (tooltips) — also escapes quotes."""
    return esc(s).replace("'", "&#39;").replace('"', "&quot;")


# Human explanations for the provenance tags shown in the watch tables. Keys are
# the exact strings _add_watch records.
PROV_HELP = {
    "capture:bdcpp+mapper": ("Seen in the real Coverity (BD/CPP) build capture: this repo "
                             "owns files the build actually compiled, attributed by the "
                             "file-to-repo mapping service."),
    "capture:recreate": ("Provisioned from ingested capture seeds (the compiled file set "
                         "plus .git checkout provenance) by the recreate pipeline."),
    "capture:gh_tree": ("Approximated from the component release tag file tree on GitHub - "
                        "a stand-in when no real build capture exists."),
    "sca:kb_vcs_url": ("Component identified by Black Duck SCA (KnowledgeBase BoM); its "
                       "upstream VCS URL resolved from the KB component identity."),
}


def _ver_digits(v):
    """Version equivalence for conflict detection: compare numeric runs only, so
    v3.6.3.1 == 3.6.3.1 and curl-8_21_0 == 8.21.0, but v3.6.3.1 != 3.2.0."""
    return re.findall(r"\d+", v or "")


def kb_conflict(w):
    """True when the SCA KB version label disagrees with the pinned ref we chose
    (i.e. local ground truth overrode sca:kb). Both sides must exist."""
    kb, pin = w.get("sca_version"), w.get("pinned_ref")
    return bool(kb and pin and _ver_digits(kb) != _ver_digits(pin))


# --------------------------------------------------------------- per-project state
class ProjectState:
    def __init__(self, data_dir: Path, triage_url: str):
        self.data_dir = data_dir
        self.triage_url = triage_url
        self.name = "?"            # registry key (defaults to project)
        self.watches = []          # watch manifest entries
        self.watch_by_url = {}     # norm_url -> watch entry
        self.watch_by_component = {}   # component -> watch entry
        self.file_index = {}       # component -> [{rel, kind, origin}]
        self.origin_index = {}     # origin -> [{component, rel}] (same physical file, N repos)
        self.results = []          # processed commit results, newest first
        self.project = "?"
        self.build_id = "?"
        self.bd_project_url = None
        self.recreate_status = {"state": "idle", "summary": None, "error": None, "at": None}
        self.recreate_lock = threading.Lock()
        self.update_status = {"state": "idle", "summary": None, "error": None, "at": None}
        self.update_lock = threading.Lock()
        self.replay_status = {"state": "idle", "summary": None, "error": None, "at": None}
        self.replay_lock = threading.Lock()
        self.fill_status = {"state": "idle", "preview": None, "summary": None,
                            "error": None, "at": None}
        self.fill_lock = threading.Lock()
        self._seen = set()          # commit shas already surfaced (replay/update dedup)
        self._load()
        self.load_rendered()        # populate results from the Postgres render cache (instant)

    def reload(self) -> None:
        """Re-read the (freshly recreated) JSON in place, keeping runtime results."""
        self.watches = []
        self.watch_by_url = {}
        self.watch_by_component = {}
        self.file_index = {}
        self.origin_index = {}
        self._load()

    # ----------------------------------------------------------- event feed (replay)
    def _events_path(self):
        return self.data_dir / f"{self.project}-commit-events.json"

    def _load_events(self):
        p = self._events_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8-sig")).get("events", [])
            except (ValueError, OSError):
                return []
        return []

    def _save_events(self, records):
        self._events_path().write_text(json.dumps(
            {"_comment": "Durable feed of surfaced upstream commit events; replayed from "
                         "cache (no tokens). Extended by 'check for updates'.",
             "events": records}, indent=2), encoding="utf-8")

    @staticmethod
    def _to_push(ev):
        return {"ref": f"refs/heads/{ev.get('branch', '')}",
                "repository": {"clone_url": ev["vcs_url"]},
                "commits": [{"id": ev["commit"], "message": ev.get("message", ""),
                             "timestamp": ev.get("committed_at"),   # GitHub push convention
                             "added": [], "removed": [], "modified": ev.get("files_changed", [])}]}

    @staticmethod
    def _tally(stats, r):
        """Partition one fired event into EXACTLY one bucket, so the buckets always sum
        to the event total (no unexplained remainder in the replay/update summary):
        in-scope -> cached | Claude-live | untriaged; out-of-scope -> suppressed (not in
        the compiled set) | reference-only (repo not monitored)."""
        stats["events"] += 1
        status = r.get("status")
        t = r.get("triage") or {}
        src = t.get("_triage_source")
        if status == "not_monitored":
            stats["not_monitored"] += 1
        elif status == "suppressed":
            stats["suppressed"] += 1
        elif status in ("triage_error", "ignored"):
            stats["errors"] += 1
        elif src == "claude-live":
            stats["live_calls"] += 1
            u = t.get("usage") or {}
            stats["tokens_in"] += u.get("input_tokens") or 0
            stats["tokens_out"] += u.get("output_tokens") or 0
        elif src == "claude-cache":
            stats["cache_hits"] += 1
        else:                                   # cache-only-failsafe / error-failsafe
            stats["untriaged"] += 1

    def _fire_events(self, records, cache_only=False, sink=None, seen=None):
        """Fire event records through the webhook path into `sink` (default self.results),
        skipping commits already in `seen` (default self._seen), and tally an EXACT
        partition (cached / Claude / untriaged / suppressed / reference-only) so the UI can
        prove a replay costs nothing AND account for every event. A separate sink/seen lets
        replay build a fresh list without disturbing the one being served."""
        if sink is None:
            sink = self.results
        if seen is None:
            seen = self._seen
        stats = {"events": 0, "live_calls": 0, "cache_hits": 0, "untriaged": 0,
                 "suppressed": 0, "not_monitored": 0, "errors": 0,
                 "tokens_in": 0, "tokens_out": 0}
        for ev in records:
            sha = ev.get("commit")
            if sha and sha in seen:
                continue
            n0 = len(sink)
            self.process_push(self._to_push(ev), cache_only=cache_only, sink=sink)
            for r in sink[:len(sink) - n0]:
                if r.get("commit"):
                    seen.add(r["commit"])
                self._tally(stats, r)
        return stats

    # ------------------------------------------------------- Postgres render cache
    def _row(self, r):
        """Shape a rendered result into a rendered_event row (index columns + full payload)."""
        return {"project_name": self.project, "commit_sha": r.get("commit") or "?",
                "component": r.get("component"), "committed_at": r.get("committed_at"),
                "label": _event_label(r), "payload": r}

    def _persist(self, results):
        """Upsert rendered results into the Postgres cache (best-effort — a cache write must
        never break the live render)."""
        try:
            from db import rendered
            rendered.save_rows([self._row(r) for r in results if r.get("commit")])
        except Exception as exc:
            print(f"[cache] persist skipped for {self.project}: {exc!r}", flush=True)

    def load_rendered(self):
        """Load this project's rendered events from Postgres into memory (the startup path
        — no per-event triage round-trip). Empty on a cold cache; a bootstrap replay fills
        it. Errors are non-fatal (fall back to an empty in-memory feed)."""
        try:
            from db import rendered
            self.results = rendered.load(self.project)
            self._seen = {r.get("commit") for r in self.results if r.get("commit")}
        except Exception as exc:
            print(f"[cache] load skipped for {self.project}: {exc!r}", flush=True)
            self.results, self._seen = [], set()

    def replay_events(self):
        """Re-render the durable feed and reconcile the Postgres cache with a MARK-AND-SWEEP,
        then atomically swap the in-memory list. Reads keep seeing the previous render until
        the swap; a stale or mis-rendered row (still pending after the re-render) is removed.
        Triage is cache-only, so a replay costs no tokens."""
        from db import rendered
        records = self._load_events()
        new_results, new_seen = [], set()
        rendered.mark_pending(self.project)                        # mark every row pending
        stats = self._fire_events(records, cache_only=True, sink=new_results, seen=new_seen)
        self._persist(new_results)                                 # upsert fresh -> clears pending
        rendered.sweep(self.project)                               # remove the still-pending stale
        self.results, self._seen = new_results, new_seen           # atomic swap (reads saw old)
        return stats

    def run_replay(self):
        try:
            stats = self.replay_events()
            self.replay_status = {"state": "done", "summary": stats, "error": None, "at": now_iso()}
        except Exception as exc:
            self.replay_status = {"state": "error", "summary": None, "error": repr(exc),
                                  "at": now_iso()}

    def is_locked(self) -> bool:
        """True while a replay or recreate is reconciling this project's render cache. The
        section is locked for WRITES (replay/update/fill/recreate/webhook are refused) but
        stays readable — the previous render is served until the reconcile swaps it in. The
        action buttons grey out while locked."""
        return (self.replay_status["state"] == "running"
                or self.recreate_status["state"] == "running")

    def _load(self) -> None:
        capture = json.loads((self.data_dir / "build-capture.json").read_text(encoding="utf-8-sig"))
        self.project = capture.get("project", "?")
        self.build_id = capture.get("build_id", "?")

        # Watch manifest from capture-detected repos (forks AND upstreams) ...
        for repo in capture.get("repos_detected", []):
            for v in repo.get("vcs_urls", []):
                self._add_watch(
                    url=v["url"],
                    component=repo.get("associated_component", "?"),
                    relationship=v.get("relationship", "?"),
                    provenance=f"capture:{v.get('found_in', '?')}",
                    pinned_ref=repo.get("pinned_ref"),
                )
                # The moving WATCH ref (branch) is distinct from the immutable
                # pinned_ref (the file-scope snapshot); carry it + provenance so
                # the manifest can show what we actually monitor.
                w = self.watch_by_url.get(norm_url(v["url"]))
                if w is not None:
                    for k in ("watch_ref", "watch_confidence", "release_style",
                              "actual_source_ref"):
                        if repo.get(k) is not None:
                            w[k] = repo[k]
                    if repo.get("built_from"):
                        w["built_from"] = repo["built_from"]
                        w["divergent"] = repo.get("divergent", False)

        # ... augmented with KB-served VCS URLs from the SCA side.
        hub_path = self.data_dir / "hub-api-components.json"
        if hub_path.exists():
            hub = json.loads(hub_path.read_text(encoding="utf-8-sig"))
            self.bd_project_url = hub.get("bdProjectUrl")
            for item in hub.get("items", []):
                url = item.get("vcsUrl")
                if not url:
                    continue
                component = item["componentName"].lower().replace(" ", "-")
                self._add_watch(
                    url=url,
                    component=component,
                    relationship="upstream",
                    provenance="sca:kb_vcs_url",
                    pinned_ref=item.get("componentVersionName"),
                )
                # Record where the version came from so the UI can flag inferred
                # values honestly: "bd" = authoritative SCA BoM; "claude-inferred"
                # = a repo BD didn't identify, version proposed by Claude.
                w = self.watch_by_url.get(norm_url(url))
                if w is not None:
                    w["version_source"] = item.get("versionSource", "bd")
                    # Keep the KB version label so the UI can show when the locally
                    # discovered ref overrode a conflicting sca:kb identification.
                    if item.get("versionSource") == "bd":
                        w["sca_version"] = item.get("componentVersionName")
                    # Provenance: we monitor the canonical, but the build may have
                    # used a local/vendored copy that diverges from it.
                    w["built_from"] = item.get("builtFrom")
                    w["divergent"] = item.get("divergent", False)
                    # Watch ref / provenance for repos that arrive only via the hub
                    # (e.g. reference-only components not in repos_detected).
                    for src, dst in (("watchRef", "watch_ref"),
                                     ("watchConfidence", "watch_confidence"),
                                     ("releaseStyle", "release_style"),
                                     ("actualSourceRef", "actual_source_ref")):
                        if item.get(src) is not None and w.get(dst) is None:
                            w[dst] = item.get(src)

        # Compiled-file index: strip each file's repo-local prefix so paths
        # are upstream-repo-relative, ready for suffix matching.
        prefixes = [(r["local_path"], r.get("associated_component", "?"))
                    for r in capture.get("repos_detected", [])]
        for f in capture.get("files", []):
            rel, comp = None, f.get("component", "?")
            for local_path, _ in prefixes:
                if f["path"].startswith(local_path + "/"):
                    rel = f["path"][len(local_path) + 1:]
                    break
            entry = {"rel": rel, "path": f["path"], "kind": f["kind"],
                     "origin": f.get("origin")}
            self.file_index.setdefault(comp, []).append(entry)
            # Mirror index: a single physical compiled file attributed to several
            # repos (host + vendored upstream) shares one `origin`. This links the
            # copies so a fix in one repo can flag the others for patching (Part 2).
            if entry["origin"] and rel is not None:
                self.origin_index.setdefault(entry["origin"], []).append(
                    {"component": comp, "rel": rel})

        # Build-observed classification (determined once, here): a watched
        # component is MONITORED iff its source was actually compiled — i.e. it
        # has files in the compiled-file index. Components in the SBOM with no
        # compiled files are reference-only (linked/prebuilt, e.g. an OpenSSL the
        # build only links) — shown for transparency but not watched. This is the
        # SBOM (recall) x compiled-set (precision) intersection at repo scope.
        for w in self.watches:
            w["monitored"] = len(self.file_index.get(w["component"], [])) > 0
            self.watch_by_component.setdefault(w["component"], w)

    def _add_watch(self, url, component, relationship, provenance, pinned_ref):
        key = norm_url(url)
        existing = self.watch_by_url.get(key)
        if existing:
            if provenance not in existing["provenance"]:
                existing["provenance"].append(provenance)
            return
        entry = {
            "url": url,
            "component": component,
            "relationship": relationship,
            "provenance": [provenance],
            "pinned_ref": pinned_ref,
        }
        self.watches.append(entry)
        self.watch_by_url[key] = entry

    # ----------------------------------------------------------- matching
    def match_file(self, component: str, changed_path: str):
        """Match one upstream-relative changed path against the compiled set.

        Tiers: 1 exact relative path (1.0), 2 trailing-segment suffix >=2
        segments (0.8), 3 basename only (0.5).
        """
        changed = changed_path.strip("/")
        cseg = changed.split("/")
        best = None
        for entry in self.file_index.get(component, []):
            rel = entry["rel"]
            if rel is None:
                continue
            if rel == changed:
                return {"path": changed, "tier": 1, "confidence": 1.0, "matched": rel,
                        "origin": entry.get("origin")}
            rseg = rel.split("/")
            n = 0
            for a, b in zip(reversed(rseg), reversed(cseg)):
                if a != b:
                    break
                n += 1
            if n >= 2 and (best is None or best["tier"] > 2):
                best = {"path": changed, "tier": 2, "confidence": 0.8, "matched": rel,
                        "origin": entry.get("origin")}
            elif n == 1 and best is None:
                best = {"path": changed, "tier": 3, "confidence": 0.5, "matched": rel,
                        "origin": entry.get("origin")}
        return best

    # ----------------------------------------------------------- triage
    def call_triage(self, vcs_url: str, commit: str, files: list, cross_repo: list = None,
                    cache_only: bool = False):
        body = {"vcs_url": vcs_url, "commit": commit, "files": files}
        if cross_repo:
            body["cross_repo"] = cross_repo
        if cache_only:
            body["cache_only"] = True
        req = urllib.request.Request(
            self.triage_url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # The stub answers in milliseconds; a live LLM triage backend
            # (claude_server.py) can take much longer on a first, uncached
            # commit, so allow generous headroom. Cached repeats are instant.
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read()), None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return None, f"triage service unreachable: {exc}"

    # ----------------------------------------------------------- webhook
    def process_push(self, payload: dict, cache_only: bool = False, sink=None) -> dict:
        target = self.results if sink is None else sink   # replay renders into its own list
        repo_url = (payload.get("repository") or {}).get("clone_url") \
            or (payload.get("repository") or {}).get("url") or ""
        ref = payload.get("ref", "")
        watch = self.watch_by_url.get(norm_url(repo_url)) if repo_url else None

        if watch is None:
            result = {
                "received_at": now_iso(), "repo": repo_url, "ref": ref,
                "status": "ignored", "reason": "repository not in watch manifest",
                "commits": [c.get("id", "?") for c in payload.get("commits", [])],
            }
            target.insert(0, result)
            return {"status": "ignored", "reason": result["reason"]}

        if not watch.get("monitored", True):
            # Reference-only component: in the SBOM but never compiled from source
            # in this build. Short-circuit at the repo level — no relevance filter,
            # no triage — rather than suppressing each commit after the fact.
            summaries = []
            for commit in payload.get("commits", []):
                sha = commit.get("id") or commit.get("sha") or "?"
                changed = (commit.get("added") or []) + (commit.get("modified") or []) \
                    + (commit.get("removed") or [])
                target.insert(0, {
                    "received_at": now_iso(), "committed_at": commit.get("timestamp"),
                    "repo": repo_url, "ref": ref,
                    "component": watch["component"], "relationship": watch["relationship"],
                    "commit": sha, "message": commit.get("message", ""),
                    "files_changed": changed, "in_scope": [], "status": "not_monitored",
                    "reason": "reference-only: linked into the build but not compiled "
                              "from source; excluded from monitoring",
                })
                summaries.append({"commit": sha, "status": "not_monitored", "verdict": None})
            return {"status": "not_monitored", "commits": summaries}

        summaries = []
        for commit in payload.get("commits", []):
            sha = commit.get("id") or commit.get("sha") or "?"
            changed = (commit.get("added") or []) + (commit.get("modified") or []) \
                + (commit.get("removed") or [])
            matches = []
            for path in changed:
                m = self.match_file(watch["component"], path)
                if m:
                    matches.append(m)

            # Cross-repo mirrors: any OTHER component holding the same physical
            # file (shared origin). A fix that lands here likely needs propagating
            # to those copies — one of them is probably behind. (Part 2)
            cross = {}
            for m in matches:
                for sib in self.origin_index.get(m.get("origin"), []):
                    if sib["component"] == watch["component"]:
                        continue
                    sw = self.watch_by_component.get(sib["component"])
                    cross.setdefault(sib["component"], {
                        "component": sib["component"], "path": sib["rel"],
                        "vcs_url": sw["url"] if sw else None,
                        "divergent": bool(sw and sw.get("divergent"))})
            cross_repo = list(cross.values())

            base = {
                "received_at": now_iso(),
                "committed_at": commit.get("timestamp"),   # the ACTUAL upstream commit date
                "repo": repo_url, "ref": ref,
                "component": watch["component"],
                "relationship": watch["relationship"],
                "commit": sha,
                "message": commit.get("message", ""),
                "files_changed": changed,
                "in_scope": matches,
                "cross_repo": cross_repo,
            }
            if not matches:
                base.update(status="suppressed",
                            reason="no changed file is in the compiled set for this build")
            else:
                verdict, err = self.call_triage(watch["url"], sha,
                                                [m["path"] for m in matches], cross_repo,
                                                cache_only=cache_only)
                if err:
                    base.update(status="triage_error", reason=err)
                else:
                    base.update(status="triaged", triage=verdict)
            target.insert(0, base)
            summaries.append({"commit": sha, "status": base["status"],
                              "verdict": base.get("triage", {}).get("verdict")})
        return {"status": "processed", "commits": summaries}

    # ----------------------------------------------------------- recreate (UI button)
    def run_recreate(self, refresh_sbom=True) -> None:
        """Refresh the BoM from Black Duck + re-run the cache-backed pipeline, then
        reload this project's manifest. Runs in a background thread; warm cache =
        near-instant, a grown BoM = a few cache misses. Status drives the UI."""
        try:
            from provisioning.recreate import recreate_project
            summary = recreate_project(self.project, self.data_dir,
                                       refresh_sbom=refresh_sbom, log=lambda m: None)
            self.reload()
            summary["replay"] = self.replay_events()   # restore the feed from cache (0 tokens)
            self.recreate_status = {"state": "done", "summary": summary,
                                    "error": None, "at": now_iso()}
        except Exception as exc:  # keep the server alive; surface the error in the UI
            self.recreate_status = {"state": "error", "summary": None,
                                    "error": repr(exc), "at": now_iso()}

    # ----------------------------------------------------------- check for updates (fetch)
    def run_update(self, component=None) -> None:
        """Fetch new commits since the cursor (local git) and show them ALL — already
        triaged ones from cache, the rest yellow/untriaged. Cache-only, so NO tokens;
        triage is the separate Fill button. Advances the cursor, so a re-check is 0 new.
        component=None fetches every watched repo; else just that one."""
        try:
            from provisioning import updater
            watches = [w for w in self.watches if component is None or w["component"] == component]
            res = updater.fetch_updates(self.project, watches, "all", None)
            before = len(self.results)
            self._fire_events(res["events"], cache_only=True)          # 0 tokens
            self._persist(self.results[:len(self.results) - before])   # cache the new rows
            existing = self._load_events()
            seen = {e.get("commit") for e in existing}
            self._save_events(existing + [e for e in res["events"] if e.get("commit") not in seen])
            updater.commit_advances(self.project, res["advances"])
            untri = sum(1 for r in self.results if _event_label(r) == "untriaged")
            self.update_status = {"state": "done", "error": None, "at": now_iso(),
                                  "summary": {"added": res["processed"], "untriaged": untri,
                                              "scope": component or "all repos",
                                              "warnings": res.get("warnings", [])}}
        except Exception as exc:
            self.update_status = {"state": "error", "summary": None, "error": repr(exc),
                                  "at": now_iso()}

    # ----------------------------------------------------------- fill missing triage (tokens)
    def run_fill(self, mode=None, limit=None, component=None) -> None:
        """Triage the untriaged (yellow) commits — the ONLY token-spending action.
        mode=None counts and auto-runs under the threshold, else parks in 'preview';
        mode 'all'/'latest' triages the chosen set (newest-first). component=None triages
        every repo's untriaged; else just that one."""
        try:
            from provisioning.updater import THRESHOLD
            untriaged = [r for r in self.results if _event_label(r) == "untriaged"
                         and (component is None or r.get("component") == component)]
            if mode is None:
                if not untriaged:
                    self.fill_status = {"state": "done", "preview": None, "error": None,
                                        "at": now_iso(),
                                        "summary": {"message": "nothing to triage",
                                                    "scope": component or "all repos"}}
                elif len(untriaged) <= THRESHOLD:
                    self._apply_fill(untriaged, component)
                else:
                    self.fill_status = {"state": "preview", "summary": None, "error": None,
                                        "at": now_iso(),
                                        "preview": {"total": len(untriaged), "threshold": THRESHOLD,
                                                    "component": component}}
            else:
                chosen = untriaged[:limit] if (mode == "latest" and limit) else untriaged
                self._apply_fill(chosen, component)
        except Exception as exc:
            self.fill_status = {"state": "error", "preview": None, "summary": None,
                                "error": repr(exc), "at": now_iso()}

    def _apply_fill(self, results, component=None) -> None:
        stats = {"triaged": 0, "live_calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0}
        for r in results:
            watch = self.watch_by_component.get(r.get("component"))
            if not watch:
                continue
            files = [m["path"] for m in r.get("in_scope", [])]
            verdict, err = self.call_triage(watch["url"], r["commit"], files,
                                            r.get("cross_repo"), cache_only=False)
            if err or not verdict:
                continue
            r["triage"], r["status"] = verdict, "triaged"
            stats["triaged"] += 1
            src = verdict.get("_triage_source")
            if src == "claude-live":
                stats["live_calls"] += 1
                u = verdict.get("usage") or {}
                stats["tokens_in"] += u.get("input_tokens") or 0
                stats["tokens_out"] += u.get("output_tokens") or 0
            elif src == "claude-cache":
                stats["cache_hits"] += 1
        self._persist(results)              # persist the newly-triaged rows to the cache
        stats["scope"] = component or "all repos"
        self.fill_status = {"state": "done", "preview": None, "error": None, "at": now_iso(),
                            "summary": stats}

    # ----------------------------------------------------------- summary
    def summary(self) -> dict:
        comps = {w["component"] for w in self.watches}
        mon = {w["component"] for w in self.watches if w.get("monitored")}
        # Count by the SAME label the project page filters/colours by, so an untriaged
        # (cache-only-failsafe) event is its own bucket and NOT folded into needs-review
        # — the list-page numbers then line up with the project-page chips.
        labels = Counter(_event_label(r) for r in self.results)
        # Newest UPSTREAM commit date in the feed (not when we replayed it), so the list
        # doesn't show every project as "active today".
        last = (self.results[0].get("committed_at") or self.results[0].get("received_at")) \
            if self.results else "—"
        return {"name": self.name, "project": self.project, "build_id": self.build_id,
                "components": len(comps), "monitored": len(mon),
                "reference_only": len(comps) - len(mon), "events": len(self.results),
                "alerts": labels.get("response_required", 0),
                "needs_review": labels.get("needs_human_review", 0),
                "untriaged": labels.get("untriaged", 0), "last_activity": last}


# --------------------------------------------------------------- registry
class Registry:
    def __init__(self, data_dirs, triage_url: str):
        self.triage_url = triage_url
        self.projects = {}         # name -> ProjectState (insertion order)
        self.add_status = {"state": "idle", "message": None, "error": None, "at": None}
        self.add_lock = threading.Lock()
        for d in data_dirs:
            ps = ProjectState(Path(d), triage_url)
            self._register(ps)
            self._remember(ps)

    def _remember(self, ps: "ProjectState") -> None:
        """Persist project -> data_dir so the next restart reloads the exact dir in use
        (best-effort; a corrupt/missing manifest just falls back to the live-* scan)."""
        try:
            m = json.loads(LOADED_MANIFEST.read_text(encoding="utf-8")) \
                if LOADED_MANIFEST.exists() else {}
            m[ps.project] = str(ps.data_dir.resolve())
            LOADED_MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _register(self, ps: "ProjectState") -> str:
        name, base, i = ps.project, ps.project, 2
        while name in self.projects:
            name = f"{base} ({i})"
            i += 1
        ps.name = name
        self.projects[name] = ps
        return name

    def add_project(self, data_dir) -> str:
        """Load a project's data dir into the running monitor (no restart)."""
        return self._register(ProjectState(Path(data_dir), self.triage_url))

    def db_projects_not_loaded(self):
        """Names of projects seeded in Postgres that aren't loaded in the monitor yet."""
        try:
            from db.session import SessionLocal
            from db.models import Project
            with SessionLocal() as s:
                names = [p.name for p in s.query(Project).all()]
        except Exception:
            return []
        loaded = {ps.project for ps in self.projects.values()}
        return [n for n in names if n not in loaded]

    def autoload_db_projects(self, log=print):
        """Startup: reload every DB-seeded project that has recreated data on disk, so
        projects added at runtime (POST /projects/add) survive a monitor restart.

        The data-dir name isn't derivable from the project name (e.g. curl lives in
        live-stage3, not live-repo-mon-stage3-curl), so we scan REPO_ROOT/live-* for a
        build-capture.json and load a dir only if its project is currently seeded in
        Postgres and not already loaded from an explicit --data-dir. Truncating the DB
        therefore gives a clean slate on the next restart; the on-disk artifacts linger
        harmlessly until their project is re-ingested."""
        try:
            from db.session import SessionLocal
            from db.models import Project
            with SessionLocal() as s:
                db_names = {p.name for p in s.query(Project).all()}
        except Exception as exc:
            log(f"[autoload] DB unavailable ({exc!r}); loading only explicit --data-dirs")
            return
        loaded_dirs = {ps.data_dir.resolve() for ps in self.projects.values()}

        def _try_load(d, why):
            d = Path(d)
            if d.resolve() in loaded_dirs or not (d / "build-capture.json").exists():
                return
            try:
                ps = ProjectState(d, self.triage_url)
            except Exception as exc:
                log(f"[autoload] skip {d.name}: {exc!r}")
                return
            if ps.project not in db_names:
                log(f"[autoload] skip {d.name}: project {ps.project!r} not seeded in DB")
                return
            if ps.project in {p.project for p in self.projects.values()}:
                return                                     # same project under another dir
            nm = self._register(ps)
            self._remember(ps)
            loaded_dirs.add(d.resolve())
            # ps loaded its rendered events from Postgres in __init__ — INSTANT, no triage.
            # Only a cold cache (empty rows but a non-empty feed) needs a bootstrap replay.
            booted = self._bootstrap_if_cold(ps)
            log(f"[autoload] loaded {nm} <- {d.name} ({why}; {len(ps.results)} cached event(s)"
                + (", bootstrapping feed in background" if booted else "") + ")")

        # 1) exact dirs remembered from the last run — authoritative, no ambiguity.
        try:
            manifest = json.loads(LOADED_MANIFEST.read_text(encoding="utf-8")) \
                if LOADED_MANIFEST.exists() else {}
        except Exception:
            manifest = {}
        for proj, d in manifest.items():
            if proj in db_names:
                _try_load(d, "remembered")

        # 2) fallback: any DB project still not loaded -> scan live-* for its data dir.
        if set(db_names) - {p.project for p in self.projects.values()}:
            for cap in sorted(REPO_ROOT.glob("live-*/build-capture.json")):
                _try_load(cap.parent, "scan")

    def _bootstrap_if_cold(self, ps):
        """If the render cache is cold (no rows loaded) but the durable feed has events,
        kick a one-time background replay to populate Postgres. Warm cache -> no-op, so a
        restart is instant and only the first run (or a project ingested before the cache
        existed) pays the replay cost. Background, so a slow triage service can't stall
        startup."""
        if not ps.results and ps._load_events():
            threading.Thread(target=ps.run_replay, daemon=True).start()
            return True
        return False

    def run_add(self, project_name, data_dir=None):
        """Load an ALREADY-recreated project data dir into the running monitor. The
        analysis script (ingest -> recreate) writes the data dir, then calls
        POST /projects/add to register it live — no restart. If the project is already
        loaded, reload it in place (idempotent re-analysis)."""
        try:
            import re
            if not data_dir:
                slug = re.sub(r"[^a-z0-9._-]+", "-", project_name.lower()).strip("-")
                data_dir = REPO_ROOT / f"live-{slug}"
            data_dir = Path(data_dir)
            if not (data_dir / "build-capture.json").exists():
                raise FileNotFoundError(f"no recreated data at {data_dir}; run recreate first")
            for nm, ps in list(self.projects.items()):
                if ps.project == project_name:
                    ps.data_dir = data_dir
                    ps.reload()
                    ps.replay_events()
                    self._remember(ps)            # remember the (possibly new) dir
                    self.add_status = {"state": "done", "message": f"reloaded {nm}",
                                       "error": None, "at": now_iso()}
                    return
            name = self.add_project(data_dir)     # loads rendered cache in __init__ (instant)
            self._remember(self.projects[name])   # survive a restart
            self._bootstrap_if_cold(self.projects[name])   # replay only if the cache is cold
            self.add_status = {"state": "done", "message": f"added {name}", "error": None,
                               "at": now_iso()}
        except Exception as exc:
            self.add_status = {"state": "error", "message": None, "error": repr(exc),
                               "at": now_iso()}

    def ingest_and_load(self, payload: dict) -> dict:
        """Remote ingestion entry point. Persist a posted seed payload to Postgres
        SYNCHRONOUSLY (so the client's POST returns only once the DB write succeeded),
        then recreate the project's artifacts + load it live in the BACKGROUND. Nothing
        here reads the build box's filesystem — the payload carries the whole seed set.
        Returns the persist summary; recreate+load progress lands in add_status."""
        from provisioning.ingest_service import persist_ingest
        from gh_replay import GH, gh_token
        summary = persist_ingest(payload, GH(gh_token()))       # raises IngestError -> 400
        name = summary["project"]
        threading.Thread(target=self._recreate_and_add,
                         args=(name, bool(payload.get("reset_feed"))), daemon=True).start()
        return summary

    def _recreate_and_add(self, name, reset_feed=False):
        """Background: recreate a freshly-ingested project's artifacts from the DB seeds
        + cache, then load/reload it live. Recreates INTO the project's existing data dir
        when it is already loaded, else the conventional live-<slug>."""
        try:
            import re
            from provisioning.recreate import recreate_project
            existing = next((ps for ps in self.projects.values() if ps.project == name), None)
            if existing is not None:
                out_dir = existing.data_dir
            else:
                slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
                out_dir = REPO_ROOT / f"live-{slug}"
            self.add_status = {"state": "running", "message": f"recreating {name}",
                               "error": None, "at": now_iso()}
            recreate_project(name, out_dir, reset_feed=reset_feed, log=lambda m: None)
            self.run_add(name, out_dir)                         # sets add_status done + remembers
        except Exception as exc:
            self.add_status = {"state": "error", "message": None, "error": repr(exc),
                               "at": now_iso()}

    def process_push(self, payload: dict) -> dict:
        repo_url = (payload.get("repository") or {}).get("clone_url") \
            or (payload.get("repository") or {}).get("url") or ""
        key = norm_url(repo_url) if repo_url else ""
        matched = [(n, p) for n, p in self.projects.items() if key and p.watch_by_url.get(key)]
        if not matched:
            return {"status": "ignored", "reason": "repository not watched by any project",
                    "repo": repo_url}
        out = []
        for n, p in matched:
            if p.replay_status["state"] == "running" or p.recreate_status["state"] == "running":
                out.append({"project": n, "status": "busy",
                            "reason": "a replay/recreate is in progress; retry shortly"})
                continue
            before = len(p.results)
            res = p.process_push(payload)                 # live webhook -> self.results
            p._persist(p.results[:len(p.results) - before])   # persist the new rows
            out.append({"project": n, **res})
        return {"status": "routed", "projects": out}


# --------------------------------------------------------------- rendering
_STYLE = """
 body { font-family: Segoe UI, sans-serif; margin: 24px; color: #21212B; }
 a { color: #582C83; }
 h1 { font-size: 22px; } h2 { font-size: 16px; margin-top: 28px; color: #582C83; }
 table { border-collapse: collapse; width: 100%; font-size: 13px; }
 th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #ddd; }
 th { color: #582C83; }
 code { background: #f4f2f8; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
 .card { background: #fafafa; margin: 8px 0; padding: 10px 14px; border-radius: 4px; }
 .badge { color: white; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
 .muted { color: #6B6B76; font-size: 12px; margin-top: 4px; }
 .rationale { font-size: 13px; margin-top: 6px; }
 .pill { display:inline-block; min-width:18px; text-align:center; padding:1px 7px;
         border-radius:10px; font-size:12px; color:white; }
 details.changed { margin-top: 6px; font-size: 12px; }
 details.changed summary { cursor: pointer; color: #582C83; user-select: none; }
 details.changed ul { margin: 4px 0 0 0; padding-left: 18px; list-style: none; }
 details.changed li { margin: 1px 0; }
 .inscope-tag { color: #B9770E; font-size: 11px; margin-left: 6px; }
"""


def _page(title: str, body: str) -> bytes:
    return (f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>{esc(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>""").encode("utf-8")


def render_project_list(reg: Registry) -> bytes:
    def pill(n, color):
        return f"<span class='pill' style='background:{color}'>{n}</span>" if n else \
               f"<span class='muted'>0</span>"
    rows = ""
    for name, ps in reg.projects.items():
        s = ps.summary()
        rows += (
            f"<tr><td><a href='/?project={quote(name)}'><b>{esc(name)}</b></a>"
            f"<div class='muted'><code>{esc(s['build_id'])}</code></div></td>"
            f"<td>{s['components']}</td>"
            f"<td>{s['monitored']}</td>"
            f"<td>{s['reference_only']}</td>"
            f"<td>{s['events']}</td>"
            f"<td>{pill(s['alerts'], '#C0392B')}</td>"
            f"<td>{pill(s['needs_review'], '#B9770E')}</td>"
            f"<td>{pill(s['untriaged'], '#F1C40F')}</td>"
            f"<td class='muted'>{esc(_fmt_ts(s['last_activity']))}</td></tr>")
    body = f"""
<h1>RepoMonitoring <span class="muted">— {len(reg.projects)} project(s)</span></h1>
<p class="muted">Each project is one Black Duck SCA analysis. Select a project to see its
watched repositories and the upstream commit events being triaged. Triage backend:
<code>{esc(reg.triage_url)}</code>.</p>
<table>
<tr><th>Project</th><th>Components</th><th>Monitored</th><th>Reference-only</th>
<th>Events</th><th>Response&nbsp;req.</th><th>Needs&nbsp;review</th><th>Untriaged</th><th>Last activity</th></tr>
{rows}</table>"""
    return _page("RepoMonitoring — projects", body)


_LABEL_COLOR = {
    "response_required": "#C0392B", "needs_human_review": "#B9770E",
    "untriaged": "#F1C40F", "not_meaningful": "#66BB6A", "suppressed": "#2E7D32",
    "not_monitored": "#5D6D7E", "ignored": "#7F8C8D", "triage_error": "#C0392B",
}
_ACTIONABLE = ("response_required", "needs_human_review")
_UNTRIAGED_SRC = ("cache-only-failsafe", "error-failsafe")


def _event_label(r):
    """The single status label an event is filtered/coloured by. A commit that was
    fired cache-only and had no cached verdict is UNTRIAGED (yellow) — distinct from a
    real needs_human_review. Otherwise: the triage verdict, else the pipeline status."""
    t = r.get("triage") or {}
    if t.get("_triage_source") in _UNTRIAGED_SRC:
        return "untriaged"
    return t.get("verdict") or r.get("status", "?")


def _label_color(label):
    return _LABEL_COLOR.get(label, "#34495E")


def _fmt_ts(s):
    """Trim a strict-ISO timestamp to 'YYYY-MM-DD HH:MM' for display; pass anything else
    through untouched."""
    s = str(s or "")
    if len(s) >= 16 and s[4:5] == "-" and s[10:11] == "T":
        return s[:16].replace("T", " ")
    return s


def _when(r):
    """The timestamp shown on an event card. Prefer the ACTUAL upstream commit date
    (committed_at) over received_at — the latter is when we replayed/triaged it, which
    made every historical fix look like it landed today. Falls back to 'seen <received>'
    only when the commit date is unknown (e.g. a pre-fix cached record)."""
    c = r.get("committed_at")
    if c:
        return f"committed {esc(_fmt_ts(c))}"
    return f"seen {esc(_fmt_ts(r.get('received_at', '')))}"


def _tokline(s):
    """Human token/cost summary for a replay/update action — proof of no repeat spend AND
    an exact account of every event. in-scope (cached + Claude + untriaged) and out-of-
    scope (suppressed + reference-only [+ errors]) always sum to the event total."""
    if not s:
        return ""
    tk = (s.get("tokens_in", 0) or 0) + (s.get("tokens_out", 0) or 0)
    inscope = []
    if s.get("cache_hits"):
        inscope.append(f"{s['cache_hits']} cached")
    inscope.append(f"{s.get('live_calls', 0)} Claude call(s)" + (f" ~{tk // 1000}k tok" if tk else ""))
    if s.get("untriaged"):
        inscope.append(f"{s['untriaged']} untriaged")
    outscope = []
    if s.get("suppressed"):
        outscope.append(f"{s['suppressed']} suppressed")
    if s.get("not_monitored"):
        outscope.append(f"{s['not_monitored']} reference-only")
    if s.get("errors"):
        outscope.append(f"{s['errors']} error(s)")
    parts = [f"{s.get('events', 0)} events", "in scope: " + " + ".join(inscope)]
    if outscope:
        parts.append("out of scope: " + " + ".join(outscope))
    return " &middot; ".join(parts)


def render_project(reg: Registry, ps: ProjectState, status_filter: str = None,
                   comp_filter: str = None) -> bytes:
    comps_on = set(comp_filter.split(",")) if comp_filter else None   # None = all shown
    locked = ps.is_locked()   # replay/recreate reconciling -> action buttons grey out

    def _greybtn(label, pad="6px 12px", fsize=13):
        return (f"<button disabled title='locked — a replay/recreate is reconciling this "
                f"project' style='background:#d5d5d5;color:#8a8a8a;border:0;border-radius:4px;"
                f"padding:{pad};font-size:{fsize}px;cursor:not-allowed'>{label}</button>")

    lock_banner = ("<div style='background:#FCF3CF;border:1px solid #F1C40F;border-radius:4px;"
                   "padding:6px 12px;margin:10px 0;font-size:13px;color:#7D6608'>&#128274; "
                   "Replay/recreate in progress — actions are locked; the current render is "
                   "still being served and this page refreshes automatically.</div>") if locked else ""
    # While locked, poll: reload so the buttons re-enable the moment the reconcile finishes.
    lock_poll = "<script>setTimeout(function(){location.reload();},2500);</script>" if locked else ""

    def _pin(w):
        pin_txt = esc(w["pinned_ref"] or "—")
        if kb_conflict(w):
            # Local ground truth won a version conflict: bold the chosen ref and
            # show the sca:kb label it overrode.
            ref = (f"<code title='Immutable file-scope ref — the exact snapshot we "
                   f"enumerated files at'><b>{pin_txt}</b></code>"
                   f" <span class='muted' title='{attr('Black Duck KB identified version ' + str(w.get('sca_version')) + ', which conflicts with the exact ref discovered in the local .git checkout; the locally discovered ref was chosen.')}'>"
                   f"overrode sca:kb <s>{esc(w.get('sca_version'))}</s></span>")
        else:
            ref = (f"<code title='Immutable file-scope ref — the exact snapshot we "
                   f"enumerated files at'>{pin_txt}</code>")
        if w.get("version_source") == "claude-inferred":
            ref += (" <span class='muted' title='Version inferred by Claude — "
                    "Black Duck did not identify this component'>≈ inferred</span>")
        return ref

    def _watch(w):
        wr = w.get("watch_ref")
        if not wr:
            return "<span class='muted'>—</span>"
        conf, style = w.get("watch_confidence"), w.get("release_style")
        flag = " ⚠" if conf == "low" else ""
        title = f"{style or '?'} release style; Claude confidence {conf or '?'}"
        return (f"<code>{esc(wr)}</code>{flag} "
                f"<span class='muted' title='{esc(title)}'>[{esc(style or '?')}/{esc(conf or '?')}]</span>")

    def _url(w):
        u = f"<code>{esc(w['url'])}</code>"
        bf = w.get("built_from")
        if bf and norm_url(bf) != norm_url(w["url"]):
            warn = " ⚠ divergent" if w.get("divergent") else ""
            asr = w.get("actual_source_ref")
            refpart = f"@{esc(asr)}" if asr else ""
            u += (f"<br><span class='muted' title='Actual source we built from; the "
                  f"canonical upstream above is what we monitor'>↳ actual source "
                  f"<code>{esc(bf)}{refpart}</code>{warn}</span>")
        return u

    def _prov(w):
        conflict = kb_conflict(w)
        parts = []
        for tag in w["provenance"]:
            help_txt = PROV_HELP.get(tag, "Provenance of this watch entry.")
            shown = esc(tag)
            if conflict and tag.startswith("capture:"):
                help_txt += (" This signal supplied the chosen ref - it overrode the "
                             "conflicting sca:kb version label.")
                shown = f"<b>{shown}</b>"
            elif conflict and tag.startswith("sca:"):
                help_txt += (" Its version label conflicted with the locally discovered "
                             "ref and was overridden.")
            parts.append(f"<span title='{attr(help_txt)}'>{shown}</span>")
        return ", ".join(parts)

    def _check_cell(w):
        on = comps_on is None or w["component"] in comps_on
        return (f"<td style='text-align:center'><input type='checkbox' class='compfilter' "
                f"value=\"{esc(w['component'])}\" onchange='applyCompFilter()'"
                f"{' checked' if on else ''}></td>")

    def _rows(ws, actions=False, checks=False):
        return "".join(
            "<tr>"
            + (_check_cell(w) if checks else "")
            + f"<td>{esc(w['component'])}</td><td>{_url(w)}</td>"
            f"<td>{esc(w['relationship'])}</td><td>{_prov(w)}</td>"
            f"<td>{_pin(w)}</td><td>{_watch(w)}</td>"
            + (f"<td>{_repo_actions(w)}</td>" if actions else "")
            + "</tr>"
            for w in ws)

    mon = [w for w in ps.watches if w.get("monitored", True)]
    ref = [w for w in ps.watches if not w.get("monitored", True)]
    comps = {w["component"] for w in ps.watches}
    mon_comps = {w["component"] for w in mon}
    funnel = (f"SBOM (Black Duck): {len(comps)} component(s) &rarr; "
              f"compiled from source (BD/CPP): {len(mon_comps)} &rarr; "
              f"<b>monitored: {len(mon_comps)}</b> · reference-only: {len(comps) - len(mon_comps)}")
    ref_section = ""
    if ref:
        ref_section = (
            f"<h2 style='color:#7F8C8D'>Referenced, not monitored ({len(ref)})</h2>"
            f"<p class='muted'>In the SBOM but not compiled from source in this build "
            f"(linked/prebuilt) — listed for transparency, excluded from monitoring.</p>"
            f"<table><tr><th title='filter events by dependency'>&#9745;</th>"
            f"<th>Component</th><th>VCS URL</th><th>Relationship</th>"
            f"<th>Provenance</th><th>Pinned ref</th><th>Watches (branch)</th></tr>"
            f"{_rows(ref, checks=True)}</table>")

    def result_card(r):
        label = _event_label(r)
        color = _label_color(label)

        # In-scope files (the compiled ones the relevance filter matched).
        in_scope = r.get("in_scope", [])
        in_scope_paths = {m["path"] for m in in_scope}
        scope_html = ""
        if in_scope:
            files = ", ".join(f"<code>{esc(m['path'])}</code> (tier {m['tier']})" for m in in_scope)
            scope_html = f"<div class='muted'>in scope: {files}</div>"

        # Every commit's actual changed files, in a collapsible disclosure — so a
        # suppressed/not-monitored commit is readable, not just a bare dash. Files
        # that matched the compiled set are tagged.
        changed = r.get("files_changed") or []
        changed_html = ""
        if changed:
            lis = "".join(
                f"<li><code>{esc(p)}</code>"
                f"{'<span class=inscope-tag>● in scope</span>' if p in in_scope_paths else ''}</li>"
                for p in changed)
            changed_html = (f"<details class='changed'><summary>{len(changed)} changed "
                            f"file{'s' if len(changed) != 1 else ''}</summary><ul>{lis}</ul></details>")

        rationale = esc((r.get("triage") or {}).get("rationale", r.get("reason", "")))
        cross_html = ""
        cross = r.get("cross_repo") or []
        if cross:
            items = "; ".join(
                f"{esc(c['component'])} (<code>{esc(c['path'])}</code>)"
                f"{' ⚠ divergent' if c.get('divergent') else ''}" for c in cross)
            cross_html = (
                f"<div class='muted' style='color:#B9770E;margin-top:6px'>"
                f"⇄ same file is also in {items} — propagate the fix; the mirrored "
                f"copy is likely behind</div>")
        return (
            f"<div class='card' style='border-left:6px solid {color}'>"
            f"<div><span class='badge' style='background:{color}'>{esc(label)}</span> "
            f"<b>{esc(r.get('component', r.get('repo', '?')))}</b> "
            f"<code>{esc(str(r.get('commit', r.get('commits', '?'))))[:16]}</code> "
            f"<span class='muted'>{esc(r.get('ref', ''))} · {_when(r)}</span></div>"
            f"{scope_html}"
            f"<div class='rationale'>{rationale}</div>{cross_html}{changed_html}</div>")

    # Status filter (URL param, so it survives the 3 s auto-refresh). A handful of
    # 100 events are actionable; let the user hide the noise.
    active = set(status_filter.split(",")) if status_filter else None
    counts = Counter(_event_label(r) for r in ps.results)
    filtered = [r for r in ps.results
                if (active is None or _event_label(r) in active)
                and (comps_on is None or r.get("component") in comps_on)]
    untri_by_comp = Counter(r.get("component") for r in ps.results
                            if _event_label(r) == "untriaged")

    def _repo_actions(w):
        comp = w["component"]
        n = untri_by_comp.get(comp, 0)
        if locked:
            return (_greybtn("&#x21bb; updates", pad="2px 7px", fsize=11)
                    + " " + _greybtn(f"&#x2699; fill {n}", pad="2px 7px", fsize=11))
        upd = (f"<button title='fetch new commits for {esc(comp)}' onclick=\"this.disabled=true;"
               f"fetch('/update?project={quote(ps.name)}&component={quote(comp)}',{{method:'POST'}})"
               f".then(()=>setTimeout(()=>location.reload(),600))\" style='background:#1F6F78;"
               f"color:#fff;border:0;border-radius:3px;padding:2px 7px;cursor:pointer;font-size:11px;"
               f"margin-right:3px'>&#x21bb; updates</button>")
        fil = (f"<button title='fill triage for {esc(comp)}' onclick=\"this.disabled=true;"
               f"fetch('/fill?project={quote(ps.name)}&component={quote(comp)}',{{method:'POST'}})"
               f".then(()=>setTimeout(()=>location.reload(),600))\" style='background:#8E44AD;"
               f"color:#fff;border:0;border-radius:3px;padding:2px 7px;cursor:pointer;font-size:11px'>"
               f"&#x2699; fill {n}</button>")
        return upd + fil

    comp_q = f"&comp={quote(','.join(sorted(comps_on)))}" if comps_on else ""

    def _toggle_href(label):
        cur = set(active) if active else set()
        new = {label} if active is None else (cur - {label} if label in cur else cur | {label})
        if not new:
            return f"/?project={quote(ps.name)}{comp_q}"
        return f"/?project={quote(ps.name)}&status={quote(','.join(sorted(new)))}{comp_q}"

    def _chip(label, count, href, on):
        c = _label_color(label)
        style = f"background:{c};color:#fff" if on else f"background:#eee;color:{c};opacity:.55"
        return (f"<a href='{href}' style='text-decoration:none;{style};padding:2px 9px;"
                f"border-radius:11px;font-size:12px;margin:0 5px 5px 0;display:inline-block'>"
                f"{esc(label)} {count}</a>")

    filter_bar = ""
    if ps.results:
        order = ([l for l in _LABEL_COLOR if l in counts]
                 + [l for l in counts if l not in _LABEL_COLOR])
        chip_html = "".join(_chip(l, counts[l], _toggle_href(l), active is None or l in active)
                            for l in order)
        all_href = f"/?project={quote(ps.name)}{comp_q}"
        act_href = f"/?project={quote(ps.name)}&status={quote(','.join(_ACTIONABLE))}{comp_q}"
        filter_bar = (f"<div style='margin:8px 0'>{chip_html}"
                      f"<a href='{all_href}' style='font-size:12px;margin-left:6px'>all</a> &middot; "
                      f"<a href='{act_href}' style='font-size:12px'>actionable</a></div>")

    CARD_CAP = 300
    if not ps.results:
        cards = "<p class='muted'>No commit events received yet for this project.</p>"
    elif not filtered:
        cards = "<p class='muted'>No events match this filter.</p>"
    else:
        note = (f"<p class='muted'>showing newest {CARD_CAP} of {len(filtered)} — narrow with "
                f"the filter chips above.</p>" if len(filtered) > CARD_CAP else "")
        cards = note + "".join(result_card(r) for r in filtered[:CARD_CAP])

    rs = ps.recreate_status
    if rs["state"] == "running":
        st = "<span style='color:#B9770E'>recreating… refreshing BoM + cache-backed rebuild</span>"
    elif rs["state"] == "done":
        s = rs["summary"] or {}
        st = (f"<span style='color:#2E7D32'>last recreate {esc(rs['at'])}: "
              f"{s.get('external_calls', '?')} external call(s), monitored="
              f"{esc(s.get('monitored'))}</span>")
        if s.get("replay"):
            st += f" <span style='color:#2E7D32'>&middot; replayed {_tokline(s['replay'])}</span>"
        if (s.get("warnings") or []):
            st += f" <span style='color:#B9770E'>· {esc('; '.join(s['warnings']))}</span>"
    elif rs["state"] == "error":
        st = f"<span style='color:#C0392B'>recreate error: {esc(rs['error'])}</span>"
    else:
        st = "<span class='muted'>idle</span>"
    recreate_btn = _greybtn("&#x1F504; Recreate") if locked else (
        f"<button onclick=\"this.disabled=true;fetch('/recreate?project={quote(ps.name)}',"
        f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
        f"style='background:#582C83;color:#fff;border:0;border-radius:4px;padding:6px 12px;"
        f"cursor:pointer;font-size:13px'>&#x1F504; Recreate</button>")
    recreate_html = f"{recreate_btn} <span style='font-size:12px'>{st}</span>"

    # Replay: re-fire the durable events file from cache (no fetch, no tokens).
    rp = ps.replay_status
    replay_btn = _greybtn("&#x25B6; Replay cached") if locked else (
        f"<button onclick=\"this.disabled=true;fetch('/replay?project={quote(ps.name)}',"
        f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
        f"style='background:#1F6F78;color:#fff;border:0;border-radius:4px;padding:6px 12px;"
        f"cursor:pointer;font-size:13px'>&#x25B6; Replay cached</button>")
    if rp["state"] == "running":
        rpt = "<span style='color:#B9770E'>replaying cached events&hellip;</span>"
    elif rp["state"] == "done":
        rpt = f"<span style='color:#2E7D32'>replayed {_tokline(rp['summary'])}</span>"
    elif rp["state"] == "error":
        rpt = f"<span style='color:#C0392B'>replay error: {esc(rp['error'])}</span>"
    else:
        rpt = "<span class='muted'>re-fire cached events (no new fetch, no tokens)</span>"
    replay_html = f"{replay_btn} <span style='font-size:12px'>{rpt}</span>"

    # Update: fetch new upstream commits since the cursor (cache-only, no tokens).
    us = ps.update_status
    update_btn = _greybtn("&#x21bb; Check for updates") if locked else (
        f"<button onclick=\"this.disabled=true;fetch('/update?project={quote(ps.name)}',"
        f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
        f"style='background:#1F6F78;color:#fff;border:0;border-radius:4px;padding:6px 12px;"
        f"cursor:pointer;font-size:13px'>&#x21bb; Check for updates</button>")
    if us["state"] == "running":
        ust = "<span style='color:#B9770E'>fetching new upstream commits&hellip;</span>"
    elif us["state"] == "done":
        s = us["summary"] or {}
        ust = (f"<span style='color:#2E7D32'>[{esc(s.get('scope', 'all repos'))}] added "
               f"{s.get('added', 0)} new commit(s) &middot; {s.get('untriaged', 0)} untriaged</span>")
        if s.get("warnings"):
            ust += f" <span style='color:#B9770E'>&middot; {esc('; '.join(s['warnings']))}</span>"
    elif us["state"] == "error":
        ust = f"<span style='color:#C0392B'>update error: {esc(us['error'])}</span>"
    else:
        ust = "<span class='muted'>fetch new commits (local git, no tokens); untriaged show yellow</span>"
    update_html = f"{update_btn} <span style='font-size:12px'>{ust}</span>"

    # Fill missing triage: the ONLY token-spending action; count-then-confirm over threshold.
    def _fbtn(label, q, bg):
        if locked:
            return _greybtn(label, pad="5px 10px", fsize=12)
        return (f"<button onclick=\"this.disabled=true;fetch('/fill?project={quote(ps.name)}{q}',"
                f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
                f"style='background:{bg};color:#fff;border:0;border-radius:4px;padding:5px 10px;"
                f"cursor:pointer;font-size:12px;margin-right:4px'>{label}</button>")
    fs = ps.fill_status
    fill_btn = _fbtn(f"&#x2699; Fill missing triage ({counts.get('untriaged', 0)})", "", "#8E44AD")
    if fs["state"] in ("counting", "running"):
        fst = "<span style='color:#B9770E'>triaging&hellip; (Claude)</span>"
    elif fs["state"] == "preview":
        p = fs["preview"] or {}
        thr = p.get("threshold", 100)
        comp = p.get("component")
        cq = f"&amp;component={quote(comp)}" if comp else ""
        scope = f" for {esc(comp)}" if comp else ""
        fst = (f"<div style='margin-top:6px;color:#C0392B'><b>{p.get('total')} untriaged</b> commits"
               f"{scope} &mdash; this spends Claude tokens. "
               + _fbtn(f"Triage all {p.get('total')}", f"&amp;mode=all{cq}", "#C0392B")
               + _fbtn(f"Only latest {thr}", f"&amp;mode=latest&amp;limit={thr}{cq}", "#B9770E")
               + _fbtn("Cancel", "&amp;mode=cancel", "#7F8C8D") + "</div>")
    elif fs["state"] == "done":
        s = fs["summary"] or {}
        sc = f"[{esc(s.get('scope', 'all repos'))}] "
        if s.get("message"):
            fst = f"<span style='color:#2E7D32'>{sc}{esc(s['message'])}</span>"
        else:
            tk = (s.get("tokens_in", 0) or 0) + (s.get("tokens_out", 0) or 0)
            fst = (f"<span style='color:#2E7D32'>{sc}triaged {s.get('triaged', 0)} "
                   f"&middot; {s.get('live_calls', 0)} Claude call(s)"
                   + (f" ~{tk // 1000}k tok" if tk else "")
                   + (f" &middot; {s['cache_hits']} cached" if s.get("cache_hits") else "") + "</span>")
    elif fs["state"] == "error":
        fst = f"<span style='color:#C0392B'>fill error: {esc(fs['error'])}</span>"
    else:
        fst = "<span class='muted'>run Claude triage on the yellow (untriaged) commits</span>"
    fill_html = f"{fill_btn} <span style='font-size:12px'>{fst}</span>"

    body = f"""
<p><a href="/">&larr; Projects</a></p>
<h1>{esc(ps.name)} <span class="muted">({esc(ps.build_id)})</span></h1>
{f'<p style="margin-top:-6px"><a href="{esc(ps.bd_project_url)}" target="_blank" title="Open this project version in Black Duck SCA">View in Black Duck SCA &#8599;</a></p>' if ps.bd_project_url else ""}
{lock_banner}
<p>{recreate_html}</p>
<p>{replay_html}</p>
<p>{update_html}</p>
<p>{fill_html}</p>
<p class="muted" style="font-size:13px"><b>Precision funnel:</b> {funnel}</p>
<h2>Monitored repos ({len(mon)})</h2>
<table><tr><th title="select all — filter the event feed by dependency"><input type="checkbox" onchange="toggleAllComp(this)"{' checked' if comps_on is None else ''}></th><th>Component</th><th>VCS URL</th><th>Relationship</th><th>Provenance</th><th>Pinned ref</th><th>Watches (branch)</th><th>Actions</th></tr>
{_rows(mon, actions=True, checks=True)}</table>
{ref_section}
<h2>Commit events ({len(filtered)}{f' of {len(ps.results)}' if (active or comps_on) else ''})</h2>
{filter_bar}
{cards}
<script>
function applyCompFilter(){{
  var boxes=[].slice.call(document.querySelectorAll('.compfilter'));
  var on=boxes.filter(function(b){{return b.checked;}}).map(function(b){{return b.value;}});
  var u=new URL(location.href);
  if(on.length===boxes.length){{u.searchParams.delete('comp');}}
  else{{u.searchParams.set('comp', on.join(','));}}
  location.href=u.toString();
}}
function toggleAllComp(src){{
  [].slice.call(document.querySelectorAll('.compfilter')).forEach(function(b){{b.checked=src.checked;}});
  applyCompFilter();
}}
</script>
{lock_poll}"""
    return _page(f"RepoMonitoring — {ps.name}", body)


# --------------------------------------------------------------- http server
class MonitorHandler(BaseHTTPRequestHandler):
    reg: Registry = None

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, indent=2).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        project = unquote(qs["project"][0]) if "project" in qs else None

        if parsed.path == "/":
            if project:
                ps = self.reg.projects.get(project)
                if ps is None:
                    self._send_json(404, {"error": f"unknown project {project}"})
                else:
                    status = unquote(qs["status"][0]) if "status" in qs else None
                    comp = unquote(qs["comp"][0]) if "comp" in qs else None
                    self._send(200, render_project(self.reg, ps, status, comp),
                               "text/html; charset=utf-8")
            else:
                self._send(200, render_project_list(self.reg), "text/html; charset=utf-8")
        elif parsed.path == "/health":
            self._send_json(200, {"status": "ok", "service": "repo-monitor",
                                  "projects": len(self.reg.projects)})
        elif parsed.path == "/api/projects":
            self._send_json(200, [p.summary() for p in self.reg.projects.values()])
        elif parsed.path == "/api/db-projects":
            self._send_json(200, {"available": self.reg.db_projects_not_loaded(),
                                  "add_status": self.reg.add_status})
        elif parsed.path == "/api/watches":
            ps = self.reg.projects.get(project)
            self._send_json(200, ps.watches if ps else
                            {n: p.watches for n, p in self.reg.projects.items()})
        elif parsed.path == "/api/results":
            ps = self.reg.projects.get(project)
            self._send_json(200, ps.results if ps else
                            {n: p.results for n, p in self.reg.projects.items()})
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        # API (called by the build-box ingest client): persist a posted seed payload to
        # Postgres, then recreate + load the project. The build box shares NO filesystem
        # with the monitor — everything needed is in the body. The SBoM is NOT sent; it is
        # reloaded from the BD link and cached. Persist is synchronous; recreate+load runs
        # in the background (poll GET /api/db-projects -> add_status).
        if parsed.path == "/projects/ingest":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "body must be valid JSON"})
                return
            try:
                summary = self.reg.ingest_and_load(payload)
            except Exception as exc:
                from provisioning.ingest_service import IngestError
                code = 400 if isinstance(exc, IngestError) else 500
                self._send_json(code, {"error": str(exc), "type": type(exc).__name__})
                return
            self._send_json(200, {"status": "ingested", "recreate": "started", **summary})
            return

        # API (called by the analysis script): register an already-recreated project's
        # data dir into the running monitor — no restart. Reloads if already loaded.
        if parsed.path == "/projects/add":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            data_dir = unquote(qs["data_dir"][0]) if "data_dir" in qs else None
            if not project:
                self._send_json(400, {"error": "missing project"})
                return
            with self.reg.add_lock:
                if self.reg.add_status["state"] == "running":
                    self._send_json(409, {"status": "busy"})
                    return
                self.reg.add_status = {"state": "running", "message": f"adding {project}",
                                       "error": None, "at": now_iso()}
            threading.Thread(target=self.reg.run_add, args=(project, data_dir), daemon=True).start()
            self._send_json(202, {"status": "started", "project": project})
            return

        # UI action: recreate a project's artifacts from DB seeds + cache (background).
        if parsed.path == "/recreate":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
                return
            if ps.is_locked():
                self._send_json(409, {"status": "busy",
                                      "reason": "replay/recreate in progress; try again shortly"})
                return
            with ps.recreate_lock:
                if ps.recreate_status["state"] == "running":
                    self._send_json(409, {"status": "busy"})
                    return
                ps.recreate_status = {"state": "running", "summary": None,
                                      "error": None, "at": now_iso()}
            threading.Thread(target=ps.run_recreate, kwargs={"refresh_sbom": True},
                             daemon=True).start()
            self._send_json(202, {"status": "started", "project": project})
            return

        # UI action: replay the durable events file from cache (no fetch, no tokens).
        if parsed.path == "/replay":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
                return
            if ps.is_locked():
                self._send_json(409, {"status": "busy",
                                      "reason": "replay/recreate in progress; try again shortly"})
                return
            with ps.replay_lock:
                if ps.replay_status["state"] == "running":
                    self._send_json(409, {"status": "busy"})
                    return
                ps.replay_status = {"state": "running", "summary": None, "error": None,
                                    "at": now_iso()}
            threading.Thread(target=ps.run_replay, daemon=True).start()
            self._send_json(202, {"status": "started", "project": project})
            return

        # UI action: fetch new upstream commits since the cursor (cache-only, no tokens).
        # Optional &component scopes it to one monitored repo; else all.
        if parsed.path == "/update":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            component = unquote(qs["component"][0]) if "component" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
                return
            if ps.is_locked():
                self._send_json(409, {"status": "busy",
                                      "reason": "replay/recreate in progress; try again shortly"})
                return
            with ps.update_lock:
                if ps.update_status["state"] == "running":
                    self._send_json(409, {"status": "busy"})
                    return
                ps.update_status = {"state": "running", "summary": None, "error": None,
                                    "at": now_iso()}
            threading.Thread(target=ps.run_update, kwargs={"component": component},
                             daemon=True).start()
            self._send_json(202, {"status": "started"})
            return

        # UI action: fill missing triage (the ONLY token-spending action) — confirm > threshold.
        # Optional &component scopes it to one monitored repo; else all.
        if parsed.path == "/fill":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            component = unquote(qs["component"][0]) if "component" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
                return
            if ps.is_locked():
                self._send_json(409, {"status": "busy",
                                      "reason": "replay/recreate in progress; try again shortly"})
                return
            mode = qs.get("mode", [None])[0]
            limit = int(qs["limit"][0]) if "limit" in qs else None
            if mode == "cancel":
                ps.fill_status = {"state": "idle", "preview": None, "summary": None,
                                  "error": None, "at": now_iso()}
                self._send_json(200, {"status": "cancelled"})
                return
            with ps.fill_lock:
                if ps.fill_status["state"] in ("counting", "running"):
                    self._send_json(409, {"status": "busy"})
                    return
                ps.fill_status = {"state": ("counting" if mode is None else "running"),
                                  "preview": ps.fill_status.get("preview"), "summary": None,
                                  "error": None, "at": now_iso()}
            threading.Thread(target=ps.run_fill,
                             kwargs={"mode": mode, "limit": limit, "component": component},
                             daemon=True).start()
            self._send_json(202, {"status": "started", "mode": mode})
            return

        if parsed.path != "/webhook":
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        event_type = self.headers.get("X-GitHub-Event", "push")
        if event_type == "ping":
            self._send_json(200, {"status": "pong"})
            return
        if event_type != "push":
            self._send_json(202, {"status": "ignored",
                                  "reason": f"unsupported event type: {event_type}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "body must be valid JSON"})
            return
        self._send_json(200, self.reg.process_push(payload))

    def log_message(self, fmt, *args):
        print(f"[monitor] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8378)
    parser.add_argument("--data-dir", type=Path, action="append", dest="data_dirs",
                        help="a project data dir (repeatable; one per BD SCA project). "
                             "Optional — projects can also be added live via POST /projects/add.")
    parser.add_argument("--triage-url", default=DEFAULT_TRIAGE_URL)
    args = parser.parse_args()

    MonitorHandler.reg = Registry(args.data_dirs or [], args.triage_url)
    MonitorHandler.reg.autoload_db_projects()   # reload runtime-added projects after restart
    print(f"[monitor] {len(MonitorHandler.reg.projects)} project(s):")
    for name, ps in MonitorHandler.reg.projects.items():
        print(f"[monitor]   {name}: {len(ps.watches)} repos, "
              f"{sum(len(v) for v in ps.file_index.values())} indexed files")
    print(f"[monitor] dashboard http://127.0.0.1:{args.port}/  "
          f"webhook POST http://127.0.0.1:{args.port}/webhook")
    # Threaded so the dashboard stays responsive while a live triage call is in
    # flight (a cold Claude triage can take tens of seconds); a single-threaded
    # server would block every GET behind the webhook being processed.
    ThreadingHTTPServer(("127.0.0.1", args.port), MonitorHandler).serve_forever()


if __name__ == "__main__":
    main()
