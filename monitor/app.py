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
                             "added": [], "removed": [], "modified": ev.get("files_changed", [])}]}

    def _fire_events(self, records, cache_only=False):
        """Fire event records through the webhook path, skipping already-seen commits,
        and tally Claude spend (live calls vs cache hits) so the UI can prove a replay
        costs nothing. cache_only=True (replay) never calls Claude on a triage miss."""
        stats = {"events": 0, "live_calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0}
        for ev in records:
            sha = ev.get("commit")
            if sha and sha in self._seen:
                continue
            n0 = len(self.results)
            self.process_push(self._to_push(ev), cache_only=cache_only)
            for r in self.results[:len(self.results) - n0]:
                if r.get("commit"):
                    self._seen.add(r["commit"])
                t = r.get("triage") or {}
                src = t.get("_triage_source")
                if src == "claude-live":
                    stats["live_calls"] += 1
                    u = t.get("usage") or {}
                    stats["tokens_in"] += u.get("input_tokens") or 0
                    stats["tokens_out"] += u.get("output_tokens") or 0
                elif src == "claude-cache":
                    stats["cache_hits"] += 1
            stats["events"] += 1
        return stats

    def replay_events(self):
        """Reset the feed and re-fire the durable events file (triage from cache -> 0
        tokens). Restores 'current state' for free."""
        self.results = []
        self._seen = set()
        return self._fire_events(self._load_events(), cache_only=True)

    def run_replay(self):
        try:
            stats = self.replay_events()
            self.replay_status = {"state": "done", "summary": stats, "error": None, "at": now_iso()}
        except Exception as exc:
            self.replay_status = {"state": "error", "summary": None, "error": repr(exc),
                                  "at": now_iso()}

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
    def process_push(self, payload: dict, cache_only: bool = False) -> dict:
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
            self.results.insert(0, result)
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
                self.results.insert(0, {
                    "received_at": now_iso(), "repo": repo_url, "ref": ref,
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
            self.results.insert(0, base)
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
    def run_update(self) -> None:
        """Fetch new commits since the cursor (local git) and show them ALL — already
        triaged ones from cache, the rest yellow/untriaged. Cache-only, so NO tokens;
        triage is the separate Fill button. Advances the cursor, so a re-check is 0 new."""
        try:
            from provisioning import updater
            res = updater.fetch_updates(self.project, self.watches, "all", None)
            self._fire_events(res["events"], cache_only=True)          # 0 tokens
            existing = self._load_events()
            seen = {e.get("commit") for e in existing}
            self._save_events(existing + [e for e in res["events"] if e.get("commit") not in seen])
            updater.commit_advances(self.project, res["advances"])
            untri = sum(1 for r in self.results if _event_label(r) == "untriaged")
            self.update_status = {"state": "done", "error": None, "at": now_iso(),
                                  "summary": {"added": res["processed"], "untriaged": untri,
                                              "warnings": res.get("warnings", [])}}
        except Exception as exc:
            self.update_status = {"state": "error", "summary": None, "error": repr(exc),
                                  "at": now_iso()}

    # ----------------------------------------------------------- fill missing triage (tokens)
    def run_fill(self, mode=None, limit=None) -> None:
        """Triage the untriaged (yellow) commits — the ONLY token-spending action.
        mode=None counts and auto-runs under the threshold, else parks in 'preview';
        mode 'all'/'latest' triages the chosen set (newest-first)."""
        try:
            from provisioning.updater import THRESHOLD
            untriaged = [r for r in self.results if _event_label(r) == "untriaged"]
            if mode is None:
                if not untriaged:
                    self.fill_status = {"state": "done", "preview": None, "error": None,
                                        "at": now_iso(), "summary": {"message": "nothing to triage"}}
                elif len(untriaged) <= THRESHOLD:
                    self._apply_fill(untriaged)
                else:
                    self.fill_status = {"state": "preview", "summary": None, "error": None,
                                        "at": now_iso(),
                                        "preview": {"total": len(untriaged), "threshold": THRESHOLD}}
            else:
                chosen = untriaged[:limit] if (mode == "latest" and limit) else untriaged
                self._apply_fill(chosen)
        except Exception as exc:
            self.fill_status = {"state": "error", "preview": None, "summary": None,
                                "error": repr(exc), "at": now_iso()}

    def _apply_fill(self, results) -> None:
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
        self.fill_status = {"state": "done", "preview": None, "error": None, "at": now_iso(),
                            "summary": stats}

    # ----------------------------------------------------------- summary
    def summary(self) -> dict:
        comps = {w["component"] for w in self.watches}
        mon = {w["component"] for w in self.watches if w.get("monitored")}
        alerts = sum(1 for r in self.results
                     if (r.get("triage") or {}).get("verdict") == "response_required")
        review = sum(1 for r in self.results
                     if (r.get("triage") or {}).get("verdict") == "needs_human_review")
        last = self.results[0]["received_at"] if self.results else "—"
        return {"name": self.name, "project": self.project, "build_id": self.build_id,
                "components": len(comps), "monitored": len(mon),
                "reference_only": len(comps) - len(mon), "events": len(self.results),
                "alerts": alerts, "needs_review": review, "last_activity": last}


# --------------------------------------------------------------- registry
class Registry:
    def __init__(self, data_dirs, triage_url: str):
        self.triage_url = triage_url
        self.projects = {}         # name -> ProjectState (insertion order)
        self.add_status = {"state": "idle", "message": None, "error": None, "at": None}
        self.add_lock = threading.Lock()
        for d in data_dirs:
            self._register(ProjectState(Path(d), triage_url))

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
                    self.add_status = {"state": "done", "message": f"reloaded {nm}",
                                       "error": None, "at": now_iso()}
                    return
            name = self.add_project(data_dir)
            self.projects[name].replay_events()   # restore any cached events (0 tokens)
            self.add_status = {"state": "done", "message": f"added {name}", "error": None,
                               "at": now_iso()}
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
        return {"status": "routed",
                "projects": [{"project": n, **p.process_push(payload)} for n, p in matched]}


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
            f"<td class='muted'>{esc(s['last_activity'])}</td></tr>")
    body = f"""
<h1>RepoMonitoring <span class="muted">— {len(reg.projects)} project(s)</span></h1>
<p class="muted">Each project is one Black Duck SCA analysis. Select a project to see its
watched repositories and the upstream commit events being triaged. Triage backend:
<code>{esc(reg.triage_url)}</code>.</p>
<table>
<tr><th>Project</th><th>Components</th><th>Monitored</th><th>Reference-only</th>
<th>Events</th><th>Response&nbsp;req.</th><th>Needs&nbsp;review</th><th>Last activity</th></tr>
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


def _tokline(s):
    """Human token/cost summary for a replay/update action — proof of no repeat spend."""
    if not s:
        return ""
    tk = (s.get("tokens_in", 0) or 0) + (s.get("tokens_out", 0) or 0)
    parts = [f"{s.get('events', 0)} events",
             f"{s.get('live_calls', 0)} Claude call(s)" + (f" ~{tk // 1000}k tok" if tk else "")]
    if s.get("cache_hits"):
        parts.append(f"{s['cache_hits']} cached")
    return " &middot; ".join(parts)


def render_project(reg: Registry, ps: ProjectState, status_filter: str = None) -> bytes:
    def _pin(w):
        ref = (f"<code title='Immutable file-scope ref — the exact snapshot we "
               f"enumerated files at'>{esc(w['pinned_ref'] or '—')}</code>")
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

    def _rows(ws):
        return "".join(
            f"<tr><td>{esc(w['component'])}</td><td>{_url(w)}</td>"
            f"<td>{esc(w['relationship'])}</td><td>{esc(', '.join(w['provenance']))}</td>"
            f"<td>{_pin(w)}</td><td>{_watch(w)}</td></tr>"
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
            f"<table><tr><th>Component</th><th>VCS URL</th><th>Relationship</th>"
            f"<th>Provenance</th><th>Pinned ref</th><th>Watches (branch)</th></tr>"
            f"{_rows(ref)}</table>")

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
            f"<span class='muted'>{esc(r.get('ref', ''))} · {esc(r['received_at'])}</span></div>"
            f"{scope_html}"
            f"<div class='rationale'>{rationale}</div>{cross_html}{changed_html}</div>")

    # Status filter (URL param, so it survives the 3 s auto-refresh). A handful of
    # 100 events are actionable; let the user hide the noise.
    active = set(status_filter.split(",")) if status_filter else None
    counts = Counter(_event_label(r) for r in ps.results)
    filtered = [r for r in ps.results if active is None or _event_label(r) in active]

    def _toggle_href(label):
        cur = set(active) if active else set()
        new = {label} if active is None else (cur - {label} if label in cur else cur | {label})
        if not new:
            return f"/?project={quote(ps.name)}"
        return f"/?project={quote(ps.name)}&status={quote(','.join(sorted(new)))}"

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
        all_href = f"/?project={quote(ps.name)}"
        act_href = f"/?project={quote(ps.name)}&status={quote(','.join(_ACTIONABLE))}"
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
    recreate_html = (
        f"<button onclick=\"this.disabled=true;fetch('/recreate?project={quote(ps.name)}',"
        f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
        f"style='background:#582C83;color:#fff;border:0;border-radius:4px;padding:6px 12px;"
        f"cursor:pointer;font-size:13px'>&#x1F504; Recreate</button> <span style='font-size:12px'>{st}</span>")

    # Replay: re-fire the durable events file from cache (no fetch, no tokens).
    rp = ps.replay_status
    replay_btn = (f"<button onclick=\"this.disabled=true;fetch('/replay?project={quote(ps.name)}',"
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
    update_btn = (f"<button onclick=\"this.disabled=true;fetch('/update?project={quote(ps.name)}',"
                  f"{{method:'POST'}}).then(()=>setTimeout(()=>location.reload(),500))\" "
                  f"style='background:#1F6F78;color:#fff;border:0;border-radius:4px;padding:6px 12px;"
                  f"cursor:pointer;font-size:13px'>&#x21bb; Check for updates</button>")
    if us["state"] == "running":
        ust = "<span style='color:#B9770E'>fetching new upstream commits&hellip;</span>"
    elif us["state"] == "done":
        s = us["summary"] or {}
        ust = (f"<span style='color:#2E7D32'>added {s.get('added', 0)} new commit(s) "
               f"&middot; {s.get('untriaged', 0)} untriaged</span>")
        if s.get("warnings"):
            ust += f" <span style='color:#B9770E'>&middot; {esc('; '.join(s['warnings']))}</span>"
    elif us["state"] == "error":
        ust = f"<span style='color:#C0392B'>update error: {esc(us['error'])}</span>"
    else:
        ust = "<span class='muted'>fetch new commits (local git, no tokens); untriaged show yellow</span>"
    update_html = f"{update_btn} <span style='font-size:12px'>{ust}</span>"

    # Fill missing triage: the ONLY token-spending action; count-then-confirm over threshold.
    def _fbtn(label, q, bg):
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
        fst = (f"<div style='margin-top:6px;color:#C0392B'><b>{p.get('total')} untriaged</b> commits "
               f"&mdash; this spends Claude tokens. "
               + _fbtn(f"Triage all {p.get('total')}", "&amp;mode=all", "#C0392B")
               + _fbtn(f"Only latest {thr}", f"&amp;mode=latest&amp;limit={thr}", "#B9770E")
               + _fbtn("Cancel", "&amp;mode=cancel", "#7F8C8D") + "</div>")
    elif fs["state"] == "done":
        s = fs["summary"] or {}
        if s.get("message"):
            fst = f"<span style='color:#2E7D32'>{esc(s['message'])}</span>"
        else:
            tk = (s.get("tokens_in", 0) or 0) + (s.get("tokens_out", 0) or 0)
            fst = (f"<span style='color:#2E7D32'>triaged {s.get('triaged', 0)} "
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
<p>{recreate_html}</p>
<p>{replay_html}</p>
<p>{update_html}</p>
<p>{fill_html}</p>
<p class="muted" style="font-size:13px"><b>Precision funnel:</b> {funnel}</p>
<h2>Monitored repos ({len(mon)})</h2>
<table><tr><th>Component</th><th>VCS URL</th><th>Relationship</th><th>Provenance</th><th>Pinned ref</th><th>Watches (branch)</th></tr>
{_rows(mon)}</table>
{ref_section}
<h2>Commit events ({len(filtered)}{f' of {len(ps.results)}' if active else ''})</h2>
{filter_bar}
{cards}"""
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
                    self._send(200, render_project(self.reg, ps, status),
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
        if parsed.path == "/update":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
                return
            with ps.update_lock:
                if ps.update_status["state"] == "running":
                    self._send_json(409, {"status": "busy"})
                    return
                ps.update_status = {"state": "running", "summary": None, "error": None,
                                    "at": now_iso()}
            threading.Thread(target=ps.run_update, daemon=True).start()
            self._send_json(202, {"status": "started"})
            return

        # UI action: fill missing triage (the ONLY token-spending action) — confirm > threshold.
        if parsed.path == "/fill":
            qs = parse_qs(parsed.query)
            project = unquote(qs["project"][0]) if "project" in qs else None
            ps = self.reg.projects.get(project)
            if ps is None:
                self._send_json(404, {"error": f"unknown project {project}"})
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
            threading.Thread(target=ps.run_fill, kwargs={"mode": mode, "limit": limit},
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
                        required=True,
                        help="a project data dir (repeatable; one per BD SCA project)")
    parser.add_argument("--triage-url", default=DEFAULT_TRIAGE_URL)
    args = parser.parse_args()

    MonitorHandler.reg = Registry(args.data_dirs, args.triage_url)
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
