"""Recreate a project's dashboard artifacts from Postgres seeds + the Redis cache.

Full pipeline, every external call routed through the cache: a WARM cache recreates
with ZERO external calls; a KB-added component is a cache miss that fetches once.
Reads the three seed tables (project / capture / source_provenance); writes the JSON
the monitor already loads (build-capture.json, hub-api-components.json, <events>.json).

    python provisioning/recreate.py --project repo-mon-stage3-curl --out-dir live-stage3
        [--refresh-sbom]     # re-pull the BD SBoM to pick up KB growth
        [--refresh-events]   # re-pull commits on the watch branches

Also importable: recreate_project(name, out_dir, ...) -> summary  (used by the UI button).
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from db.models import Project                                          # noqa: E402
from db.session import SessionLocal                                    # noqa: E402
from services import cache                                            # noqa: E402
import repo_mapper                                                    # noqa: E402
from attribute_capture import (BUILD_TOOLS, HDR_EXT, claude_from_files,  # noqa: E402
                               digest_tails, fetch_fileset, norm_repo,
                               resolve_tag, resolve_watch_refs, slugify)
from bd_scout import BDClient, load_config                            # noqa: E402
from bd_provision import (bd_ui_url, build_anthropic_client, component_context,  # noqa: E402
                          enhance_with_claude, resolve_project_version)
from gh_replay import GH, gh_token, fetch_commits, parse_owner_repo    # noqa: E402


def _clean_tag(source_ref):
    """Extract a clean release tag from a `.git` actual-source ref like
    '68720b48 (curl-8_21_0)'. Returns None if HEAD wasn't exactly on a tag (a
    describe like 'v1.3.1-1-g59933ec') — in that case fall back to the SCA version."""
    m = re.search(r"\(([^)]+)\)", source_ref or "")
    if not m:
        return None
    desc = m.group(1)
    return None if re.search(r"-\d+-g[0-9a-f]+$", desc) else desc


def recreate_project(name, out_dir, refresh_sbom=False, refresh_events=False,
                     commits=6, reset_feed=False, log=print):
    cache.reset_stats()
    cfg = load_config()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Re-base cleanup: when the monitored release changes (e.g. curl 8.11 -> 8.21) the
    # accumulated event feed + cursors are relative to the OLD tag and are now stale.
    # --reset-feed clears them so the feed starts fresh at the new pinned release.
    if reset_feed:
        from db.models import EventCursor
        (out_dir / f"{name}-commit-events.json").write_text(
            json.dumps({"_comment": "Durable event feed (reset).", "events": []}, indent=2),
            encoding="utf-8")
        with SessionLocal() as s:
            n = s.query(EventCursor).filter_by(project_name=name).delete()
            s.commit()
        from db import rendered
        rows = rendered.clear(name)          # drop the materialized render cache too
        log(f"[reset-feed] cleared events file + {n} cursor(s) + {rows} rendered row(s)")

    # ---------- seeds from Postgres ----------
    with SessionLocal() as s:
        proj = s.query(Project).filter_by(name=name).one_or_none()
        if proj is None:
            raise SystemExit(f"no ingested project {name!r} (run provisioning/ingest.py)")
        cap = proj.captures[-1]
        primaries = {f.path for f in cap.files if f.is_primary}
        all_compiled = {f.path for f in cap.files}
        provenance = [{"ground_truth_url": p.ground_truth_url,
                       "actual_source_url": p.actual_source_url,
                       "actual_source_ref": p.actual_source_ref,
                       "divergent": p.divergent} for p in proj.provenance]
        bd_version = proj.bd_version
    log(f"[seed] {len(primaries)} primary TUs, {len(all_compiled)} compiled files, "
        f"{len(provenance)} provenance rows")

    # lazily-built external clients — only created if a cache MISS needs them
    clients = {}
    def _gh():   return clients.setdefault("gh", GH(gh_token()))
    def _bd():   return clients.setdefault("bd", BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False)))
    def _anth(): return clients.setdefault("anth", build_anthropic_client(cfg["anthropic_api_key"]))

    # ---------- (1) SBoM (refreshable — the KB-growth source) ----------
    def sbom_producer():
        proj, ver, comps = resolve_project_version(_bd(), name, bd_version)
        return {"components": component_context(comps),
                "bd_project_url": bd_ui_url(cfg["url"], proj, ver)}
    raw = cache.cached("sbom", {"project": name, "version": bd_version},
                       sbom_producer, refresh=refresh_sbom)
    # Tolerate the pre-link cache shape (a bare component list): no link until a
    # refresh repopulates the cache.
    sbom = raw["components"] if isinstance(raw, dict) else raw
    bd_project_url = raw.get("bd_project_url") if isinstance(raw, dict) else None
    log(f"[sbom] {len(sbom)} components")

    # ---------- (2) vcs-enhance, per component (only new ones miss) ----------
    def vcs_batch(missing_keys):
        idx = {(c["componentName"].lower(), c["componentVersionName"].lower()): c for c in sbom}
        ctx = [idx[(k[0].lower(), k[1].lower())] for k in missing_keys
               if (k[0].lower(), k[1].lower()) in idx]
        resolved, _ = enhance_with_claude(_anth(), ctx)
        by = {(r.component_name.lower(), r.component_version.lower()): r for r in resolved}
        out = {}
        for k in missing_keys:
            r = by.get((k[0].lower(), k[1].lower()))
            out[k] = ({"vcs_url": r.vcs_url, "default_branch": r.default_branch,
                       "confidence": r.confidence, "rationale": r.rationale} if r
                      else {"vcs_url": None, "default_branch": None,
                            "confidence": "none", "rationale": "no resolution"})
        return out
    vcs_keys = [(c["componentName"], c["componentVersionName"]) for c in sbom]
    vcs = cache.cached_many("vcs", vcs_keys, vcs_batch)

    candidates = {}   # norm_repo -> candidate dict
    for c in sbom:
        v = vcs[(c["componentName"], c["componentVersionName"])]
        url = v.get("vcs_url")
        if url and norm_repo(url):
            candidates[norm_repo(url)] = {
                "name": c["componentName"], "slug": slugify(c["componentName"]),
                "vcs_url": url, "version": c["componentVersionName"], "version_source": "bd"}

    # ---------- (3) Claude reconstruction from compiled paths (union) ----------
    tails = digest_tails(all_compiled)
    tails_sha = hashlib.sha256(json.dumps(tails, sort_keys=True).encode()).hexdigest()

    def fromfiles_producer():
        comps, _ = claude_from_files(cfg, tails)
        return [{"name": c.name, "vcs_url": c.vcs_url, "proposed_version": c.proposed_version,
                 "confidence": c.confidence, "rationale": c.rationale} for c in comps]
    fromfiles = cache.cached("fromfiles", {"tails_sha": tails_sha}, fromfiles_producer)
    for c in fromfiles:
        if (c["name"] or "").strip().lower() in BUILD_TOOLS:
            continue
        nk = norm_repo(c["vcs_url"])
        if nk and nk not in candidates:
            candidates[nk] = {"name": c["name"], "slug": slugify(c["name"]), "vcs_url": c["vcs_url"],
                              "version": c["proposed_version"], "version_source": "claude-inferred"}

    # ---------- (4) provenance triple from Postgres (no live .git) ----------
    for p in provenance:
        nc = norm_repo(p["ground_truth_url"])
        prov = {"actual_source": {"repo": p["actual_source_url"], "ref": p["actual_source_ref"]},
                "ground_truth": p["ground_truth_url"], "divergent": p["divergent"]}
        if nc in candidates:
            candidates[nc].update(prov)
        else:
            owner, repo = parse_owner_repo(p["ground_truth_url"])
            candidates[nc] = {"name": repo or nc, "slug": slugify(repo or nc),
                              "vcs_url": p["ground_truth_url"], "version": None,
                              "version_source": "git-discovered", **prov}

    # ---------- (5) repo trees at the immutable file-scope ref (cached) ----------
    # Prefer the EXACT tag we actually built (git provenance) over BD's component
    # version string, which can be an unreliable KB label (e.g. curl "rc-8_21_0-2").
    filesets, warnings = {}, []
    for cand in candidates.values():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if not owner:
            continue
        asrc = cand.get("actual_source")
        pin = _clean_tag(asrc.get("ref")) if (asrc and not cand.get("divergent")) else None

        def tree_producer(o=owner, r=repo, ver=cand.get("version"), pin=pin):
            try:
                tag = pin or resolve_tag(_gh(), o, r, ver)
                ref = tag or _gh().get(f"/repos/{o}/{r}").get("default_branch", "master")
                fs, _ = fetch_fileset(_gh(), o, r, ref)
                return {"ref": ref, "files": sorted(fs)}
            except Exception as exc:   # stale ref, missing repo, etc. — surface, don't abort
                return {"ref": None, "files": [], "error": str(exc)}

        tree = cache.cached("tree", {"repo": f"{owner}/{repo}", "ref": pin or cand.get("version")},
                            tree_producer)
        cand["file_scope_ref"] = tree["ref"]
        if tree.get("error"):
            warnings.append(f"{cand['slug']}: recorded ref no longer resolves upstream ({tree['error']})")
        filesets[cand["slug"]] = set(tree["files"])

    # ---------- (6) watch refs (Claude, per repo@version, only new miss) ----------
    def watch_batch(missing_keys):
        targets, keymap = [], {}
        for k in missing_keys:
            owner, repo = k[0].split("/", 1)
            targets.append({"slug": k[0], "owner": owner, "repo": repo, "version": k[1]})
            keymap[k[0]] = k
        wr = resolve_watch_refs(_gh(), _anth(), targets)
        out = {}
        for slug, w in wr.items():
            out[keymap[slug]] = {"watch_branch": w.watch_branch, "release_style": w.release_style,
                                 "confidence": w.confidence, "rationale": w.rationale}
        for k in missing_keys:
            out.setdefault(k, {"watch_branch": None, "release_style": "unknown",
                               "confidence": "low", "rationale": "unresolved"})
        return out
    watch_keys, key_for = [], {}
    for cand in candidates.values():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if owner:
            k = (f"{owner}/{repo}", cand.get("version"))
            key_for[cand["slug"]] = k
            watch_keys.append(k)
    watch = cache.cached_many("watch", list(dict.fromkeys(watch_keys)), watch_batch)
    for cand in candidates.values():
        w = watch.get(key_for.get(cand["slug"]))
        if w:
            cand.update(watch_ref=w["watch_branch"], release_style=w["release_style"],
                        watch_confidence=w["confidence"], watch_rationale=w["rationale"])

    # ---------- (7) attribution (multi-map) + classification ----------
    mapper = repo_mapper.attribute(sorted(all_compiled), filesets)
    combined = {p: [(a.repo, a.rel, a.kind) for a in mapper.get(p, [])] for p in all_compiled}
    monitored = sorted({slug for p in primaries for (slug, _, _) in combined.get(p, [])})
    log(f"[classify] monitored={monitored}")

    files_out = []
    for cp in sorted(all_compiled):
        origin = hashlib.sha1(cp.replace("\\", "/").lower().encode()).hexdigest()[:12]
        for (slug, rel, how) in combined.get(cp, []):
            if slug in monitored:
                files_out.append({"path": f"{slug}/{rel}", "component": slug,
                                  "kind": "header" if cp.lower().endswith(HDR_EXT) else "source",
                                  "resolution": how, "origin": origin})

    slug_by = {c["slug"]: c for c in candidates.values()}
    repos_detected = []
    for slug in monitored:
        c = slug_by[slug]
        entry = {"local_path": slug, "associated_component": slug,
                 "pinned_ref": c.get("file_scope_ref") or c.get("version"),
                 "watch_ref": c.get("watch_ref"), "watch_confidence": c.get("watch_confidence"),
                 "release_style": c.get("release_style"),
                 "vcs_urls": [{"url": c["vcs_url"], "relationship": "upstream", "found_in": "recreate"}]}
        asrc = c.get("actual_source")
        if asrc:
            entry["built_from"] = asrc["repo"]
            entry["actual_source_ref"] = asrc["ref"]
            entry["divergent"] = c.get("divergent", False)
        repos_detected.append(entry)

    (out_dir / "build-capture.json").write_text(json.dumps({
        "_comment": "Recreated from Postgres seeds + Redis cache (no rebuild, no idir).",
        "project": name, "build_id": f"{name}@{bd_version}",
        "repos_detected": repos_detected, "files": files_out}, indent=2), encoding="utf-8")

    items = []
    for c in candidates.values():
        it = {"componentName": c["name"], "componentVersionName": c.get("version") or "?",
              "vcsUrl": c["vcs_url"], "versionSource": c["version_source"],
              "monitored_hint": c["slug"] in monitored,
              "watchRef": c.get("watch_ref"), "watchConfidence": c.get("watch_confidence"),
              "releaseStyle": c.get("release_style"), "fileScopeRef": c.get("file_scope_ref"),
              "fallback": {"component": c["name"], "version": c.get("version"), "source": c["version_source"]}}
        asrc = c.get("actual_source")
        if asrc:
            it["builtFrom"] = asrc["repo"]
            it["actualSourceRef"] = asrc["ref"]
            it["divergent"] = c.get("divergent", False)
        items.append(it)
    (out_dir / "hub-api-components.json").write_text(json.dumps({
        "_comment": "Union watch manifest (recreated).",
        "project": name, "version": bd_version, "bdProjectUrl": bd_project_url,
        "totalCount": len(items), "items": items},
        indent=2), encoding="utf-8")

    # Events are NOT fetched here — recreate rebuilds the MANIFEST only. Upstream
    # commit events are owned by the 'check for updates' backfill + the durable events
    # file, and replayed from cache (the monitor's Replay button / auto-replay after a
    # recreate). That is why a recreate spends nothing on events.

    summary = {
        "project": name, "monitored": monitored,
        "reference_only": sorted(c["slug"] for c in candidates.values() if c["slug"] not in monitored),
        "components": len(candidates),
        "external_calls": cache.external_calls(),
        "misses": dict(cache.STATS["miss"]), "hits": dict(cache.STATS["hit"]),
        "warnings": warnings, "out_dir": str(out_dir)}
    for w in warnings:
        log(f"[warn] {w}")
    log(f"[done] external_calls={summary['external_calls']} misses={summary['misses']} "
        f"-> {out_dir}")
    return summary


def register_with_monitor(monitor_url, project, out_dir):
    """POST the recreated project to a running monitor so it loads it without a
    restart. Non-fatal if the monitor isn't up."""
    import urllib.parse
    import urllib.request
    url = (monitor_url.rstrip("/") + "/projects/add?"
           + urllib.parse.urlencode({"project": project, "data_dir": str(Path(out_dir).resolve())}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b""),
                                    timeout=30) as r:
            print(f"[monitor] registered {project}: {r.read().decode()}", flush=True)
    except Exception as exc:
        print(f"[monitor] could not register with {monitor_url} ({exc}); "
              f"start the monitor or load --data-dir {out_dir} manually", flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--refresh-sbom", action="store_true")
    ap.add_argument("--refresh-events", action="store_true")
    ap.add_argument("--commits", type=int, default=6)
    ap.add_argument("--reset-feed", action="store_true",
                    help="clear the durable event feed + cursors (use when RE-BASING a "
                         "project to a new release, so the old gap's commits don't linger)")
    ap.add_argument("--monitor-url", default=None,
                    help="if set, POST the recreated project to this monitor to load it "
                         "live (e.g. http://127.0.0.1:8378)")
    args = ap.parse_args()
    summary = recreate_project(args.project, args.out_dir, refresh_sbom=args.refresh_sbom,
                               refresh_events=args.refresh_events, commits=args.commits,
                               reset_feed=args.reset_feed)
    print(json.dumps(summary, indent=2))
    if args.monitor_url:
        register_with_monitor(args.monitor_url, args.project, args.out_dir)


if __name__ == "__main__":
    main()
