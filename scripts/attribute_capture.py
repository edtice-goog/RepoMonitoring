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
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_scout import load_config, BDClient             # noqa: E402
from bd_provision import (resolve_project_version, component_context,   # noqa: E402
                          enhance_with_claude, build_anthropic_client)
from gh_replay import GH, gh_token, parse_owner_repo   # noqa: E402
import repo_mapper                                     # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EMIT = Path(r"C:\Data\repo-monitoring-workspace\bdcpp-output\cov_emit_links.json")
DEFAULT_DIR = REPO_ROOT / "live-stage3"
MODEL = "claude-opus-4-6"
SRC_EXT = (".c", ".cc", ".cpp", ".cxx")
HDR_EXT = (".h", ".hpp", ".hh", ".hxx")

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


def _is_scaffolding(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return "/cmakefiles/" in p or "/compilerid" in p


def parse_emit(emit_path: Path):
    d = json.loads(emit_path.read_text(encoding="utf-8"))
    id2path = {f["id"]: (f.get("case-preserved") or f.get("case-normalized") or "")
               for f in d.get("files", []) if isinstance(f, dict) and "id" in f}
    primaries, all_compiled = set(), set()
    for tu in d.get("translation-units", []):
        prim = id2path.get(tu.get("primary-file-id"), "")
        if prim.lower().endswith(SRC_EXT) and not _is_scaffolding(prim):
            primaries.add(prim)
        for inf in tu.get("input-files", []):
            p = id2path.get(inf.get("file-id"), "")
            if p.lower().endswith(SRC_EXT + HDR_EXT) and not _is_scaffolding(p):
                all_compiled.add(p)
    return primaries, all_compiled


def digest_tails(paths, depth=5, cap=500):
    tails = set()
    for p in paths:
        segs = [s for s in p.replace("\\", "/").split("/") if s]
        tails.add("/".join(segs[-depth:]))
    return sorted(tails)[:cap]


def norm_repo(url):
    return re.sub(r"^https?://", "", (url or "").strip().lower()).rstrip("/").removesuffix(".git")


def slugify(name):
    return name.lower().replace(" ", "-")


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
def discover_git(gh, paths):
    """For each compiled file, find the enclosing .git checkout and its origin
    remote (ground-truth: the file IS that repo's code). Resolve each checkout to
    its canonical upstream via the GitHub fork-parent. Returns:
        git_attr : compiled_path -> (canonical_norm, repo_rel_path)
        checkouts: canonical_norm -> {canonical_url, checkout_url, divergent, files}
    """
    dir_cache = {}     # dir -> (checkout_root, origin_url) or (None, None)
    canon_cache = {}   # checkout_url -> canonical_url

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
                url = None
                try:
                    url = subprocess.check_output(["git", "-C", d, "remote", "get-url", "origin"],
                                                  text=True, stderr=subprocess.DEVNULL).strip()
                except Exception:
                    pass
                r = (d, url)
                for s in seen:
                    dir_cache[s] = r
                return r
            d = os.path.dirname(d)
        for s in seen:
            dir_cache[s] = (None, None)
        return (None, None)

    def canonical(url):
        if url in canon_cache:
            return canon_cache[url]
        can, (owner, repo) = url, parse_owner_repo(url)
        if owner:
            try:
                info = gh.get(f"/repos/{owner}/{repo}")
                par = info.get("parent") or info.get("source") or {}
                if par.get("full_name"):
                    can = f"https://github.com/{par['full_name']}"
            except Exception:
                pass
        canon_cache[url] = can
        return can

    git_attr, checkouts = {}, {}
    for p in paths:
        root, url = root_remote(p)
        if not url:
            continue
        can = canonical(url)
        nc = norm_repo(can)
        rel = p.replace("\\", "/")[len(root.replace("\\", "/")) + 1:] if root else p
        git_attr[p] = (nc, rel)
        info = checkouts.setdefault(nc, {"canonical_url": can, "checkout_url": url,
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
    _, _, bom = resolve_project_version(bd, args.project, args.version)
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

    # (3) .git discovery on the actual checkouts (ground-truth + fork->canonical).
    git_attr, checkouts = discover_git(gh, all_compiled)
    print(f"[git]  {len(checkouts)} checkout(s) discovered on disk:", flush=True)
    for nc, info in checkouts.items():
        div = "  DIVERGENT (built from a fork/vendored copy)" if info["divergent"] else ""
        print(f"    - built from {info['checkout_url']} -> canonical {info['canonical_url']}{div}", flush=True)
        if nc in candidates:                       # same component -> record provenance
            candidates[nc]["built_from"] = info["checkout_url"]
            candidates[nc]["divergent"] = info["divergent"]
        else:                                       # union: never drop a discovered repo
            owner, repo = parse_owner_repo(info["canonical_url"])
            candidates[nc] = {"name": repo or nc, "slug": slugify(repo or nc),
                              "vcs_url": info["canonical_url"], "version": None,
                              "version_source": "git-discovered",
                              "built_from": info["checkout_url"], "divergent": info["divergent"]}
    print(f"[union] {len(candidates)} candidate repos", flush=True)

    # (4) enumerate each candidate repo's file tree (for the longest-suffix fallback).
    filesets = {}
    for cand in candidates.values():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if not owner:
            continue
        try:
            tag = resolve_tag(gh, owner, repo, cand["version"])
            ref = tag or gh.get(f"/repos/{owner}/{repo}").get("default_branch", "master")
            fs, _ = fetch_fileset(gh, owner, repo, ref)
        except Exception as exc:
            # A candidate whose repo can't be resolved (e.g. a BoM component
            # mapped to a non-existent GitHub repo) is skipped for suffix
            # attribution but kept in the SBOM/union — never fatal to the run.
            print(f"    ! {owner}/{repo}: skipped, tree unavailable ({exc})", flush=True)
            continue
        cand["ref"] = ref
        filesets[cand["slug"]] = fs

    # (5) attribution: .git ground-truth first, longest-suffix fallback.
    slug_of_nc = {nc: c["slug"] for nc, c in candidates.items()}
    need_suffix = [p for p in all_compiled if p not in git_attr]
    suffix_attr = repo_mapper.attribute(need_suffix, filesets)
    combined = {}   # path -> (slug, rel, how)
    for p in all_compiled:
        if p in git_attr:
            nc, rel = git_attr[p]
            combined[p] = (slug_of_nc.get(nc), rel, "git")
        else:
            a = suffix_attr.get(p)
            combined[p] = ((a.repo, a.rel, "suffix") if a else None)

    # (6) monitored iff a PRIMARY TU landed on the repo (canonical slug).
    monitored = sorted({combined[p][0] for p in primaries
                        if combined.get(p) and combined[p][0]})
    print(f"[classify] monitored (own >=1 primary TU): {monitored}", flush=True)

    files = []
    for cp, c in sorted(combined.items()):
        if c and c[0] in monitored:
            files.append({"path": f"{c[0]}/{c[1]}", "component": c[0],
                          "kind": "header" if cp.lower().endswith(HDR_EXT) else "source",
                          "resolution": c[2]})

    slug_by = {c["slug"]: c for c in candidates.values()}
    repos_detected = []
    for slug in monitored:
        c = slug_by[slug]
        entry = {"local_path": slug, "associated_component": slug,
                 "pinned_ref": c.get("ref") or c.get("version"),
                 "vcs_urls": [{"url": c["vcs_url"], "relationship": "upstream",
                               "found_in": "bdcpp+mapper"}]}
        if c.get("built_from"):
            entry["built_from"] = c["built_from"]
            entry["divergent"] = c.get("divergent", False)
        repos_detected.append(entry)
    (args.dir / "build-capture.json").write_text(json.dumps({
        "_comment": ("Compiled-file index from a REAL BD/CPP capture, attributed by "
                     ".git ground-truth (fork resolved to canonical) with longest-suffix "
                     "fallback, over union(BD BoM, Claude-from-files, .git). Anchored on "
                     "the CANONICAL repo so upstream security commits match; a divergent "
                     "local checkout is recorded as provenance, not a second watch target."),
        "project": args.project, "build_id": f"{args.project}@{args.version}",
        "repos_detected": repos_detected, "files": files,
    }, indent=2), encoding="utf-8")

    items = []
    for c in candidates.values():
        it = {"componentName": c["name"], "componentVersionName": c.get("version") or "?",
              "vcsUrl": c["vcs_url"], "versionSource": c["version_source"],
              "monitored_hint": c["slug"] in monitored}
        if c.get("built_from"):
            it["builtFrom"] = c["built_from"]
            it["divergent"] = c.get("divergent", False)
        items.append(it)
    (args.dir / "hub-api-components.json").write_text(json.dumps({
        "_comment": "Union watch manifest: Black Duck BoM UNION Claude-from-files UNION .git checkouts.",
        "project": args.project, "version": args.version,
        "totalCount": len(items), "items": items,
    }, indent=2), encoding="utf-8")

    ref_only = [c["slug"] for c in candidates.values() if c["slug"] not in monitored]
    print(f"[write] build-capture.json: {len(files)} indexed files for {monitored}", flush=True)
    print(f"[result] monitored={monitored}  reference-only={ref_only}", flush=True)
    for c in candidates.values():
        if c.get("divergent"):
            print(f"[provenance] {c['slug']}: monitoring canonical {c['vcs_url']}, "
                  f"BUILT FROM divergent local copy {c['built_from']}", flush=True)


if __name__ == "__main__":
    main()
