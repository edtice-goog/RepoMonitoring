"""RepoMonitoring — monitoring component (demo web application).

In production this component would subscribe to a notification API fed by new
BD/CPP analyses; for the demo it reads the capture and SBOM artifacts from
disk at startup. It exposes:

    GET  /            HTML dashboard (watch manifest, alerts, review queue, log)
    GET  /health      service health
    GET  /api/watches watch manifest as JSON
    GET  /api/results processed commit results as JSON
    POST /webhook     Git webhook (GitHub push-event format)

Webhook semantics: the commit hash is the unit of selection and triage. For
each commit in the payload, the changed files are compared against the
compiled-file index. If at least one file is in scope, the commit is selected
and ALL of its in-scope files are sent together in one call to the LLM triage
service. Commits with no in-scope files are suppressed (and logged with the
reason). Repos not in the watch manifest are ignored (and logged).

Usage:
    python app.py [--port 8378] [--data-dir ../samples]
                  [--triage-url http://127.0.0.1:8377/triage]
"""

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "samples"
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


# --------------------------------------------------------------- state
class MonitorState:
    def __init__(self, data_dir: Path, triage_url: str):
        self.data_dir = data_dir
        self.triage_url = triage_url
        self.watches = []          # watch manifest entries
        self.watch_by_url = {}     # norm_url -> watch entry
        self.file_index = {}       # component -> [{rel, kind}]
        self.results = []          # processed commit results, newest first
        self.project = "?"
        self.build_id = "?"
        self._load()

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
            entry = {"rel": rel, "path": f["path"], "kind": f["kind"]}
            self.file_index.setdefault(comp, []).append(entry)

        # Build-observed classification (determined once, here): a watched
        # component is MONITORED iff its source was actually compiled — i.e. it
        # has files in the compiled-file index. Components in the SBOM with no
        # compiled files are reference-only (linked/prebuilt, e.g. an OpenSSL the
        # build only links) — shown for transparency but not watched. This is the
        # SBOM (recall) x compiled-set (precision) intersection at repo scope.
        for w in self.watches:
            w["monitored"] = len(self.file_index.get(w["component"], [])) > 0

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
                return {"path": changed, "tier": 1, "confidence": 1.0, "matched": rel}
            rseg = rel.split("/")
            n = 0
            for a, b in zip(reversed(rseg), reversed(cseg)):
                if a != b:
                    break
                n += 1
            if n >= 2 and (best is None or best["tier"] > 2):
                best = {"path": changed, "tier": 2, "confidence": 0.8, "matched": rel}
            elif n == 1 and best is None:
                best = {"path": changed, "tier": 3, "confidence": 0.5, "matched": rel}
        return best

    # ----------------------------------------------------------- triage
    def call_triage(self, vcs_url: str, commit: str, files: list):
        req = urllib.request.Request(
            self.triage_url,
            data=json.dumps({"vcs_url": vcs_url, "commit": commit, "files": files}).encode(),
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
    def process_push(self, payload: dict) -> dict:
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

            base = {
                "received_at": now_iso(),
                "repo": repo_url, "ref": ref,
                "component": watch["component"],
                "relationship": watch["relationship"],
                "commit": sha,
                "message": commit.get("message", ""),
                "files_changed": changed,
                "in_scope": matches,
            }
            if not matches:
                base.update(status="suppressed",
                            reason="no changed file is in the compiled set for this build")
            else:
                verdict, err = self.call_triage(watch["url"], sha,
                                                [m["path"] for m in matches])
                if err:
                    base.update(status="triage_error", reason=err)
                else:
                    base.update(status="triaged", triage=verdict)
            self.results.insert(0, base)
            summaries.append({"commit": sha, "status": base["status"],
                              "verdict": base.get("triage", {}).get("verdict")})
        return {"status": "processed", "commits": summaries}


# --------------------------------------------------------------- dashboard
def render_dashboard(state: MonitorState) -> str:
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _rows(ws):
        return "".join(
            f"<tr><td>{esc(w['component'])}</td><td><code>{esc(w['url'])}</code></td>"
            f"<td>{esc(w['relationship'])}</td><td>{esc(', '.join(w['provenance']))}</td>"
            f"<td><code>{esc(w['pinned_ref'] or '—')}</code></td></tr>"
            for w in ws)

    mon = [w for w in state.watches if w.get("monitored", True)]
    ref = [w for w in state.watches if not w.get("monitored", True)]
    watch_rows = _rows(mon)
    comps = {w["component"] for w in state.watches}
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
            f"<th>Provenance</th><th>Pinned ref</th></tr>{_rows(ref)}</table>")

    def result_card(r):
        status = r["status"]
        verdict = (r.get("triage") or {}).get("verdict")
        color = {"response_required": "#C0392B", "needs_human_review": "#B9770E",
                 "not_meaningful": "#7F8C8D"}.get(verdict) or \
                {"suppressed": "#2E7D32", "ignored": "#7F8C8D",
                 "not_monitored": "#5D6D7E", "triage_error": "#C0392B"}.get(status, "#34495E")
        label = verdict or status
        files = ", ".join(f"<code>{esc(m['path'])}</code> (tier {m['tier']})"
                          for m in r.get("in_scope", [])) or "—"
        rationale = esc((r.get("triage") or {}).get("rationale", r.get("reason", "")))
        return (
            f"<div class='card' style='border-left:6px solid {color}'>"
            f"<div><span class='badge' style='background:{color}'>{esc(label)}</span> "
            f"<b>{esc(r.get('component', r.get('repo', '?')))}</b> "
            f"<code>{esc(str(r.get('commit', r.get('commits', '?'))))[:16]}</code> "
            f"<span class='muted'>{esc(r.get('ref', ''))} · {esc(r['received_at'])}</span></div>"
            f"<div class='muted'>in scope: {files}</div>"
            f"<div class='rationale'>{rationale}</div></div>")

    cards = "".join(result_card(r) for r in state.results) or \
        "<p class='muted'>No commit events received yet. POST a push payload to /webhook.</p>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>RepoMonitoring — {esc(state.project)}</title>
<style>
 body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #21212B; }}
 h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 28px; color: #582C83; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ text-align: left; padding: 5px 10px; border-bottom: 1px solid #ddd; }}
 th {{ color: #582C83; }}
 code {{ background: #f4f2f8; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
 .card {{ background: #fafafa; margin: 8px 0; padding: 10px 14px; border-radius: 4px; }}
 .badge {{ color: white; padding: 2px 8px; border-radius: 10px; font-size: 12px; }}
 .muted {{ color: #6B6B76; font-size: 12px; margin-top: 4px; }}
 .rationale {{ font-size: 13px; margin-top: 6px; }}
</style></head><body>
<h1>RepoMonitoring <span class="muted">— {esc(state.project)} ({esc(state.build_id)})</span></h1>
<p class="muted">Demo monitoring component. Data loaded from disk; in production this is fed by the
BD SCA notification API. Triage backend: <code>{esc(state.triage_url)}</code> (stub).</p>
<p class="muted" style="font-size:13px"><b>Precision funnel:</b> {funnel}</p>
<h2>Monitored repos ({len(mon)})</h2>
<table><tr><th>Component</th><th>VCS URL</th><th>Relationship</th><th>Provenance</th><th>Pinned ref</th></tr>
{watch_rows}</table>
{ref_section}
<h2>Commit events ({len(state.results)})</h2>
{cards}
</body></html>"""


# --------------------------------------------------------------- http server
class MonitorHandler(BaseHTTPRequestHandler):
    state: MonitorState = None

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, indent=2).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, render_dashboard(self.state).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "repo-monitor",
                                  "watches": len(self.state.watches)})
        elif self.path == "/api/watches":
            self._send_json(200, self.state.watches)
        elif self.path == "/api/results":
            self._send_json(200, self.state.results)
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/webhook":
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
        self._send_json(200, self.state.process_push(payload))

    def log_message(self, fmt, *args):
        print(f"[monitor] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8378)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--triage-url", default=DEFAULT_TRIAGE_URL)
    args = parser.parse_args()

    MonitorHandler.state = MonitorState(args.data_dir, args.triage_url)
    print(f"[monitor] {MonitorHandler.state.project} ({MonitorHandler.state.build_id}): "
          f"{len(MonitorHandler.state.watches)} watched repos, "
          f"{sum(len(v) for v in MonitorHandler.state.file_index.values())} indexed files")
    print(f"[monitor] dashboard http://127.0.0.1:{args.port}/  "
          f"webhook POST http://127.0.0.1:{args.port}/webhook")
    HTTPServer(("127.0.0.1", args.port), MonitorHandler).serve_forever()


if __name__ == "__main__":
    main()
