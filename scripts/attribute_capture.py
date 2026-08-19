"""Real-capture provisioning (replaces the naive directory heuristic in cov_index.py).

Determines which repos to monitor from a genuine BD/CPP capture WITHOUT assuming
files are tidily arranged by component (which, if true, would mean you didn't need
SCA at all). Candidate repos come from the UNION of three independent signals so
we never under-approximate:

  1. Black Duck BoM            - SCA content-identity (BDBA on the binary).
  2. Claude-from-compiled-files - a second content-identity, from the file paths.
  3. .git discovery            - the actual checkouts on disk. A compiled file
     inside a checkout is DEFINITIVELY that repo's code (ground-truth attribution,
     no longest-suffix guessing), and its remote tells us where it came from.

Key rule for vendored / forked copies: we MONITOR THE CANONICAL upstream, not the
local copy. A vendored copy is inert (zero activity); security patches land in the
project repo. So a discovered checkout (e.g. a fork edtice-goog/zlib) is resolved
to its canonical (fork-parent madler/zlib), which is what gets watched, and the
local copy is shown as divergent PROVENANCE - never a second monitored repo. If a
discovered repo can't be tied to a content-identified canonical, we still list it
(union: never miss); inert ones are harmless (their commits find nothing in scope).

Monitored iff a repo owns >=1 PRIMARY translation unit (a .c actually compiled).
Owning only #included headers (OpenSSL, linked prebuilt) is not compiled-from-
source -> reference-only.

Writes live-stage3/build-capture.json (compiled index, anchored on the canonical
so upstream commits match) and a union hub-api-components.json tagging each
version's source (bd | claude-inferred | git-discovered) and any divergent
built-from provenance, so the UI can be honest about what it knows.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_scout import load_config, BDClient             # noqa: E402
from bd_provision import (resolve_project_version, component_context,   # noqa: E402
                          enhance_with_claude, build_anthropic_client, bd_ui_url)
from gh_replay import GH, gh_token, parse_owner_repo   # noqa: E402
import repo_mapper                                     # noqa: E402
# Pure-stdlib local collection lives in emit_local (so a build box can import it without
# the heavy deps above). Re-exported here to keep the server pipeline's import surface.
from emit_local import (SRC_EXT, HDR_EXT, DEFAULT_EMIT, _is_scaffolding,  # noqa: E402,F401
                        parse_emit, digest_tails, norm_repo, slugify, _git,
                        files_payload, collect_checkouts_local)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO_ROOT / "live-stage3"
MODEL = "claude-opus-4-6"

SYSTEM_FROMFILES = """\
You are reconstructing a software Bill of Materials from a list of source and \
header file paths that were COMPILED FROM SOURCE in one build. Identify the \
open-source components/libraries these files belong to. For each, give the \
canonical upstream GitHub repository (https, no trailing .git) and a likely \
version (the file list rarely pins an exact version, so this is a best estimate \
- say so via confidence). Only list components you are actually confident are \
present from the file evidence. Do not invent components or repositories.

EXCLUDE build tools and toolchains - CMake, Make, Ninja, Meson, autoconf/\
automake, and compilers (MSVC, GCC, Clang, LLVM). Their presence in build paths \
(e.g. CMakeFiles/, compiler probe files) does NOT make them shipped components. \
List only libraries/software actually compiled or linked into the product."""

BUILD_TOOLS = {"cmake", "ninja", "make", "gnu make", "meson", "autoconf",
               "automake", "gcc", "clang", "llvm", "msvc", "coverity"}

# parse_emit / digest_tails / norm_repo / slugify / _is_scaffolding / _git and the file
# extension sets are imported from emit_local (above) — the stdlib-only build-box surface.


def _clean_tag(source_ref):
    """Extract a clean release tag from a `.git` actual-source ref like
    '68720b48 (curl-8_21_0)'. Returns None if HEAD wasn't exactly on a tag (a
    describe like 'v1.3.1-1-g59933ec') — in that case fall back to the SCA version."""
    m = re.search(r"\(([^)]+)\)", source_ref or "")
    if not m:
        return None
    desc = m.group(1)
    return None if re.search(r"-\d+-g[0-9a-f]+$", desc) else desc


# --------------------------------------------------------------- Claude-from-files
def claude_from_files(cfg, tails):
    from pydantic import BaseModel
    from typing import List, Literal, Optional

    class Comp(BaseModel):
        name: str
        vcs_url: Optional[str]
        proposed_version: Optional[str]
        confidence: Literal["high", "medium", "low"]
        rationale: str

    class Result(BaseModel):
        components: List[Comp]

    client = build_anthropic_client(cfg["anthropic_api_key"])
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000, system=SYSTEM_FROMFILES,
        messages=[{"role": "user", "content": json.dumps(
            {"instruction": "Reconstruct the components from these compiled file paths.",
             "compiled_file_paths": tails}, indent=2)}],
        output_format=Result,
    )
    return resp.parsed_output.components, resp.usage


# --------------------------------------------------------------- .git discovery
# (_git is imported from emit_local.)
def fork_parent(gh, url):
    """Resolve a checkout's origin URL to its canonical upstream: if it's a GitHub fork,
    the fork-parent (the repo that matters); otherwise the URL unchanged. This is the one
    NETWORK step of provenance discovery, kept server-side so the build box needs no
    GitHub token — ingest_service calls it while enriching the posted checkouts."""
    can, (owner, repo) = url, parse_owner_repo(url)
    if owner:
        try:
            info = gh.get(f"/repos/{owner}/{repo}")
            par = info.get("parent") or info.get("source") or {}
            if par.get("full_name"):
                can = f"https://github.com/{par['full_name']}"
        except Exception:
            pass
    return can


def discover_git(gh, paths):
    """For each compiled file, find the enclosing .git checkout and its origin
    remote. This is the ACTUAL SOURCE (the file literally IS this checkout's code)
    — including the exact ref built (`rev-parse HEAD` + `describe`). The checkout
    may be a fork; the GROUND TRUTH (the upstream that matters) is its fork-parent.
    Returns:
        git_attr : compiled_path -> (ground_truth_norm, repo_rel_path)
        checkouts: ground_truth_norm -> {ground_truth_url, actual_source_url,
                   actual_source_ref, divergent, files}
    """
    dir_cache = {}     # dir -> (checkout_root, origin_url) or (None, None)
    canon_cache = {}   # checkout_url -> canonical_url
    ref_cache = {}     # checkout_root -> "sha (describe)"

    def root_remote(p):
        seen, d = [], os.path.dirname(p)
        while d and d != os.path.dirname(d):
            if d in dir_cache:
                r = dir_cache[d]
                for s in seen:
                    dir_cache[s] = r
                return r
            seen.append(d)
            if os.path.isdir(os.path.join(d, ".git")):
                r = (d, _git(d, "remote", "get-url", "origin"))
                for s in seen:
                    dir_cache[s] = r
                return r
            d = os.path.dirname(d)
        for s in seen:
            dir_cache[s] = (None, None)
        return (None, None)

    def exact_ref(root):
        if root not in ref_cache:
            sha, desc = _git(root, "rev-parse", "HEAD"), _git(root, "describe", "--tags", "--always")
            ref_cache[root] = f"{(sha or '?')[:12]}" + (f" ({desc})" if desc and desc != sha else "")
        return ref_cache[root]

    def canonical(url):
        if url not in canon_cache:
            canon_cache[url] = fork_parent(gh, url)
        return canon_cache[url]

    git_attr, checkouts = {}, {}
    for p in paths:
        root, url = root_remote(p)
        if not url:
            continue
        can = canonical(url)
        nc = norm_repo(can)
        rel = p.replace("\\", "/")[len(root.replace("\\", "/")) + 1:] if root else p
        git_attr[p] = (nc, rel)
        info = checkouts.setdefault(nc, {"ground_truth_url": can, "actual_source_url": url,
                                         "actual_source_ref": exact_ref(root),
                                         "divergent": norm_repo(url) != nc, "files": set()})
        info["files"].add(p)
    return git_attr, checkouts


# --------------------------------------------------------------- GitHub trees
def resolve_tag(gh, owner, repo, version):
    v = (version or "").strip()
    if not v:
        return None
    u = v.replace(".", "_")
    for cand in (f"v{v}", v, f"R_{u}", f"OpenSSL_{u}", f"openssl-{v}",
                 f"curl-{u}", f"{repo}-{u}", f"rel-{v}", u):
        try:
            if gh.exists(f"/repos/{owner}/{repo}/git/ref/tags/{cand}"):
                return cand
        except Exception:
            pass
    return None


def fetch_fileset(gh, owner, repo, ref):
    tree = gh.get(f"/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
    return ({b["path"] for b in tree.get("tree", []) if b.get("type") == "blob"},
            tree.get("truncated", False))


# --------------------------------------------------------------- watch-branch resolver
SYSTEM_WATCH = """\
You decide WHICH BRANCH to monitor for post-release security fixes, per upstream \
repository. Key idea: the immutable release tag/commit we built is NOT what we \
watch (it never moves); we watch the MOVING branch on which fixes for that \
release line will appear.

For each target you get the repo, the version we built, the repo's branch list, \
its recent tags, and its default branch. Return, per target:
- watch_branch: the EXACT branch name (must be one of the provided branches) where \
fixes for the built version's release line land.
- release_style: "tag" if the project ships releases as tags and fixes flow onto \
a default/maintenance branch; "branch" if it maintains a per-release/maintenance \
branch (e.g. OpenSSL openssl-3.x / OpenSSL_1_1_1-stable, linux-X.Y.y); "unknown" \
if you cannot tell.
- confidence: high/medium/low.
- rationale: one sentence citing the evidence (which branch/convention).

Use your knowledge of each project's real release conventions (OpenSSL uses \
per-minor stable branches; curl releases from master and back-ports rarely; zlib \
ships tags off its default branch; the Linux kernel uses linux-X.Y.y stable \
branches). Prefer a maintenance branch matching the built major.minor when one \
exists; otherwise the default branch. NEVER invent a branch not in the list."""


def resolve_watch_refs(gh, client, targets):
    """targets: [{slug, owner, repo, version}]. Returns {slug: WatchRef pydantic}.

    Claude picks the moving branch to monitor (release conventions are project-
    specific — better than hard-coded name guessing). One batched structured call.
    """
    from pydantic import BaseModel
    from typing import List, Literal

    ctx = []
    for t in targets:
        o, r = t["owner"], t["repo"]
        def _safe(path, default):
            try:
                return gh.get(path)
            except Exception:
                return default
        branches = [b["name"] for b in _safe(f"/repos/{o}/{r}/branches?per_page=100", [])]
        tags = [x["name"] for x in _safe(f"/repos/{o}/{r}/tags?per_page=30", [])]
        default_branch = (_safe(f"/repos/{o}/{r}", {}) or {}).get("default_branch")
        ctx.append({"slug": t["slug"], "repo": f"{o}/{r}", "built_version": t.get("version"),
                    "default_branch": default_branch, "branches": branches[:100],
                    "recent_tags": tags[:30]})

    class WatchRef(BaseModel):
        slug: str
        watch_branch: str
        release_style: Literal["tag", "branch", "unknown"]
        confidence: Literal["high", "medium", "low"]
        rationale: str

    class Result(BaseModel):
        targets: List[WatchRef]

    resp = client.messages.parse(
        model=MODEL, max_tokens=max(2000, 250 * len(ctx)), system=SYSTEM_WATCH,
        messages=[{"role": "user", "content": json.dumps({"targets": ctx}, indent=2)}],
        output_format=Result)
    return {w.slug: w for w in resp.parsed_output.targets}


# --------------------------------------------------------------- main
def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", type=Path, default=DEFAULT_EMIT)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--project", default="repo-mon-stage3-curl")
    ap.add_argument("--version", default="8.11.0")
    args = ap.parse_args()

    cfg = load_config()
    primaries, all_compiled = parse_emit(args.emit)
    print(f"[emit] {len(primaries)} primary TUs, {len(all_compiled)} compiled files", flush=True)
    gh = GH(gh_token())

    # candidate repos keyed by normalized url -> dict(name, slug, vcs_url, version,
    # version_source, [built_from], [divergent])
    candidates = {}

    # (1) Black Duck content-identity (canonical).
    bd = BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False))
    bd_proj, bd_ver, bom = resolve_project_version(bd, args.project, args.version)
    bd_project_url = bd_ui_url(cfg["url"], bd_proj, bd_ver)
    anthropic_client = build_anthropic_client(cfg["anthropic_api_key"])
    bd_repos, _ = enhance_with_claude(anthropic_client, component_context(bom))
    for r in bd_repos:
        if r.vcs_url and norm_repo(r.vcs_url):
            candidates[norm_repo(r.vcs_url)] = {
                "name": r.component_name, "slug": slugify(r.component_name),
                "vcs_url": r.vcs_url, "version": r.component_version, "version_source": "bd"}
    print(f"[bd]   {len(candidates)} components in the Black Duck BoM", flush=True)

    # (2) Claude reconstructs components from the compiled file paths (union).
    comps, usage = claude_from_files(cfg, digest_tails(all_compiled))
    print(f"[claude] reconstructed {len(comps)} components (tok {usage.input_tokens}/{usage.output_tokens}):", flush=True)
    for c in comps:
        if c.name.strip().lower() in BUILD_TOOLS:
            print(f"    - {c.name}: dropped (build tool, not a shipped component)", flush=True)
            continue
        nk = norm_repo(c.vcs_url)
        new = nk and nk not in candidates
        print(f"    - {c.name} {c.proposed_version or '?'}  {c.vcs_url}  [{c.confidence}]"
              f"{'  <-- NEW (not in BD BoM)' if new else ''}", flush=True)
        if nk and new:
            candidates[nk] = {"name": c.name, "slug": slugify(c.name), "vcs_url": c.vcs_url,
                              "version": c.proposed_version, "version_source": "claude-inferred"}

    # (3) .git discovery: ACTUAL SOURCE (exact checkout + ref) -> GROUND TRUTH
    #     (fork-parent upstream). Records the provenance triple per repo.
    git_attr, checkouts = discover_git(gh, all_compiled)
    print(f"[git]  {len(checkouts)} checkout(s) discovered on disk:", flush=True)
    for nc, info in checkouts.items():
        div = "  DIVERGENT (fork/vendored copy)" if info["divergent"] else ""
        print(f"    - actual source {info['actual_source_url']}@{info['actual_source_ref']} "
              f"-> ground truth {info['ground_truth_url']}{div}", flush=True)
        prov = {"actual_source": {"repo": info["actual_source_url"],
                                  "ref": info["actual_source_ref"]},
                "ground_truth": info["ground_truth_url"], "divergent": info["divergent"]}
        if nc in candidates:
            candidates[nc].update(prov)
        else:                                       # union: never drop a discovered repo
            owner, repo = parse_owner_repo(info["ground_truth_url"])
            candidates[nc] = {"name": repo or nc, "slug": slugify(repo or nc),
                              "vcs_url": info["ground_truth_url"], "version": None,
                              "version_source": "git-discovered", **prov}
    print(f"[union] {len(candidates)} candidate repos", flush=True)

    # (4) enumerate each candidate repo's file tree at the IMMUTABLE file-scope ref
    #     (the exact release snapshot) — used only to attribute compiled files.
    filesets = {}
    for cand in candidates.values():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if not owner:
            continue
        # Prefer the EXACT tag we actually built (git provenance) over the SCA
        # component version, which can be an unreliable KB label (e.g. a 3.6.3.1
        # checkout signature-matched as "3.2.0").
        asrc = cand.get("actual_source")
        pin = _clean_tag(asrc.get("ref")) if (asrc and not cand.get("divergent")) else None
        try:
            tag = pin or resolve_tag(gh, owner, repo, cand["version"])
            ref = tag or gh.get(f"/repos/{owner}/{repo}").get("default_branch", "master")
            fs, _ = fetch_fileset(gh, owner, repo, ref)
        except Exception as exc:
            # A candidate whose repo can't be resolved (e.g. a BoM component
            # mapped to a non-existent GitHub repo) is skipped for suffix
            # attribution but kept in the SBOM/union — never fatal to the run.
            print(f"    ! {owner}/{repo}: skipped, tree unavailable ({exc})", flush=True)
            continue
        cand["file_scope_ref"] = ref
        filesets[cand["slug"]] = fs

    # (4.5) WATCH ref (moving branch) per candidate — resolved by Claude, since
    #       release conventions (tag vs per-release branch) are project-specific.
    wtargets = []
    for cand in candidates.values():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if owner:
            wtargets.append({"slug": cand["slug"], "owner": owner, "repo": repo,
                             "version": cand.get("version")})
    watch = {}
    if wtargets:
        try:
            watch = resolve_watch_refs(gh, anthropic_client, wtargets)
            print(f"[watch] Claude resolved watch branches for {len(watch)} repo(s):", flush=True)
            for slug, w in watch.items():
                print(f"    - {slug}: watch '{w.watch_branch}'  "
                      f"[{w.release_style}/{w.confidence}]  {w.rationale}", flush=True)
        except Exception as exc:
            print(f"[watch] resolver failed ({exc}); watch refs left unresolved", flush=True)
    for cand in candidates.values():
        w = watch.get(cand["slug"])
        if w:
            cand.update(watch_ref=w.watch_branch, release_style=w.release_style,
                        watch_confidence=w.confidence, watch_rationale=w.rationale)

    # (5) attribution: .git ground-truth primary + repo_mapper MULTI-map
    #     (longest-suffix owners + vendored-copy secondaries). A file may map to
    #     >1 repo (host + vendored upstream) -> each is watched.
    slug_of_nc = {nc: c["slug"] for nc, c in candidates.items()}
    mapper = repo_mapper.attribute(sorted(all_compiled), filesets)
    combined = {}   # path -> [(slug, rel, how)]
    for p in all_compiled:
        attrs, seen = [], set()
        if p in git_attr:                           # ground truth first
            slug = slug_of_nc.get(git_attr[p][0])
            if slug:
                attrs.append((slug, git_attr[p][1], "git"))
                seen.add(slug)
        for a in mapper.get(p, []):                 # + suffix/vendored, deduped
            if a.repo not in seen:
                attrs.append((a.repo, a.rel, a.kind))
                seen.add(a.repo)
        combined[p] = attrs

    # (6) monitored iff a repo owns >=1 PRIMARY TU via ANY of its attributions.
    monitored = sorted({slug for p in primaries for (slug, _, _) in combined.get(p, [])})
    print(f"[classify] monitored (own >=1 primary TU): {monitored}", flush=True)
    kinds_by_repo = defaultdict(set)
    for p in primaries:
        for (slug, _, how) in combined.get(p, []):
            kinds_by_repo[slug].add(how)
    vendored_only = sorted(s for s in monitored if kinds_by_repo[s] == {"vendored"})
    if vendored_only:
        print(f"[vendored] {vendored_only}: monitored via a vendored-copy source only "
              "(host owns the file directly; upstream is watched for the fix)", flush=True)

    # (7) per-attribution file index. One physical compiled file mapping to N repos
    #     yields N entries sharing an `origin` key so Part-2 can flag mirrored copies.
    files = []
    for cp in sorted(all_compiled):
        origin = hashlib.sha1(cp.replace("\\", "/").lower().encode()).hexdigest()[:12]
        for (slug, rel, how) in combined.get(cp, []):
            if slug in monitored:
                files.append({"path": f"{slug}/{rel}", "component": slug,
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
                 "vcs_urls": [{"url": c["vcs_url"], "relationship": "upstream",
                               "found_in": "bdcpp+mapper"}]}
        asrc = c.get("actual_source")
        if asrc:
            entry["built_from"] = asrc["repo"]
            entry["actual_source_ref"] = asrc["ref"]
            entry["divergent"] = c.get("divergent", False)
        repos_detected.append(entry)
    args.dir.mkdir(parents=True, exist_ok=True)
    (args.dir / "build-capture.json").write_text(json.dumps({
        "_comment": ("Compiled-file index from a REAL BD/CPP capture. Attributed by "
                     ".git ground-truth + repo_mapper multi-map (a file may map to >1 "
                     "repo: host + vendored upstream). Each repo carries the provenance "
                     "triple (actual source / ground truth / fallback) and a Claude- "
                     "resolved watch_ref (the moving branch), distinct from the immutable "
                     "pinned_ref used for file scope."),
        "project": args.project, "build_id": f"{args.project}@{args.version}",
        "repos_detected": repos_detected, "files": files,
    }, indent=2), encoding="utf-8")

    items = []
    for c in candidates.values():
        it = {"componentName": c["name"], "componentVersionName": c.get("version") or "?",
              "vcsUrl": c["vcs_url"], "versionSource": c["version_source"],
              "monitored_hint": c["slug"] in monitored,
              "watchRef": c.get("watch_ref"), "watchConfidence": c.get("watch_confidence"),
              "releaseStyle": c.get("release_style"), "fileScopeRef": c.get("file_scope_ref"),
              "fallback": {"component": c["name"], "version": c.get("version"),
                           "source": c["version_source"]}}
        asrc = c.get("actual_source")
        if asrc:
            it["builtFrom"] = asrc["repo"]
            it["actualSourceRef"] = asrc["ref"]
            it["divergent"] = c.get("divergent", False)
        items.append(it)
    (args.dir / "hub-api-components.json").write_text(json.dumps({
        "_comment": "Union watch manifest: Black Duck BoM UNION Claude-from-files UNION .git checkouts.",
        "project": args.project, "version": args.version, "bdProjectUrl": bd_project_url,
        "totalCount": len(items), "items": items,
    }, indent=2), encoding="utf-8")

    ref_only = [c["slug"] for c in candidates.values() if c["slug"] not in monitored]
    print(f"[write] build-capture.json: {len(files)} index entries for {monitored}", flush=True)
    print(f"[result] monitored={monitored}  reference-only={ref_only}", flush=True)
    for c in candidates.values():
        if c.get("divergent"):
            print(f"[provenance] {c['slug']}: ground truth {c['vcs_url']} @ watch "
                  f"'{c.get('watch_ref')}'; ACTUAL SOURCE (divergent) "
                  f"{c['actual_source']['repo']}@{c['actual_source']['ref']}", flush=True)


if __name__ == "__main__":
    main()
