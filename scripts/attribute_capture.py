"""Real-capture provisioning (replaces the naive directory heuristic in cov_index.py).

Determines which repos to monitor from a genuine BD/CPP capture WITHOUT assuming
files are tidily arranged by component (which, if true, would mean you didn't need
SCA at all). The pipeline:

  1. Compiled files  <- cov_emit_links.json: the primary translation units (the
     .c actually compiled) and the full compiled set (sources + included headers).
  2. Black Duck BoM  <- the SCA-identified components (recall side; already
     VCS-resolved by bd_provision into live-stage3/hub-api-components.json).
  3. Claude-from-files BoM <- ask Claude to reconstruct the components purely from
     the compiled file paths (a second, independent identification). Boosts recall
     and proposes versions for anything BD missed.
  4. Candidate repos = UNION of (2) and (3) -> a repo is only missed if BOTH miss
     it.
  5. Enumerate each candidate repo's file set at its tag (GitHub tree).
  6. attribute() every compiled file to a repo via the mapping service
     (scripts/repo_mapper.py; longest-suffix today).
  7. A repo is MONITORED iff it owns >=1 PRIMARY translation unit. Owning only
     #included headers (e.g. OpenSSL, which curl links prebuilt) is NOT compiled
     from source: the fix there is to wait for the vendor's release binary, not to
     patch-and-recompile -- out of our use case -> reference-only.

Writes live-stage3/build-capture.json (compiled index for monitored repos) and
rewrites live-stage3/hub-api-components.json as the union watch manifest, tagging
each version with its source (bd | claude-inferred) so the UI can flag inferred
values honestly.
"""

import argparse
import json
import re
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

# Belt-and-suspenders: build tools are never BoM components, so drop them even if
# the model surfaces one anyway.
BUILD_TOOLS = {"cmake", "ninja", "make", "gnu make", "meson", "autoconf",
               "automake", "gcc", "clang", "llvm", "msvc", "coverity"}


# --------------------------------------------------------------- emit parsing
def _is_scaffolding(path: str) -> bool:
    """Build-system probe compilations, not part of the product. CMake's
    compiler-detection compiles CMakeCCompilerId.c under CMakeFiles/*/CompilerId*/
    on every configure; that is the build tool checking the compiler, never a
    shipped component. Excluding it stops the build tool itself (CMake, etc.) from
    being mistaken for a monitored dependency."""
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


# --------------------------------------------------------------- Claude-from-files
def claude_from_files(cfg, tails):
    try:
        import anthropic
    except ImportError:
        sys.exit("needs the anthropic SDK: pip install -r requirements-live.txt")
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

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000, system=SYSTEM_FROMFILES,
        messages=[{"role": "user", "content": json.dumps(
            {"instruction": "Reconstruct the components from these compiled file paths.",
             "compiled_file_paths": tails}, indent=2)}],
        output_format=Result,
    )
    return resp.parsed_output.components, resp.usage


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


def norm_repo(url):
    return re.sub(r"^https?://", "", (url or "").strip().lower()).rstrip("/").removesuffix(".git")


# --------------------------------------------------------------- main
def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", type=Path, default=DEFAULT_EMIT)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                    help="live dir holding the BD hub manifest; outputs written here")
    ap.add_argument("--project", default="repo-mon-stage3-curl")
    ap.add_argument("--version", default="8.11.0")
    args = ap.parse_args()

    cfg = load_config()
    primaries, all_compiled = parse_emit(args.emit)
    print(f"[emit] {len(primaries)} primary TUs, {len(all_compiled)} compiled files", flush=True)

    # (2) Black Duck side: fetch the BoM live and VCS-resolve it (self-contained,
    # so re-runs don't read a file this script also overwrites).
    bd = BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False))
    proj, ver, bom = resolve_project_version(bd, args.project, args.version)
    anthropic_client = build_anthropic_client(cfg["anthropic_api_key"])
    bd_repos, _ = enhance_with_claude(anthropic_client, component_context(bom))

    candidates = {}   # norm_url -> {name, slug, vcs_url, version, version_source}
    for r in bd_repos:
        if not r.vcs_url or not norm_repo(r.vcs_url):
            continue
        candidates[norm_repo(r.vcs_url)] = {
            "name": r.component_name,
            "slug": r.component_name.lower().replace(" ", "-"),
            "vcs_url": r.vcs_url,
            "version": r.component_version,
            "version_source": "bd",
        }
    print(f"[bd]   {len(candidates)} components in the Black Duck BoM", flush=True)

    # (3) Claude reconstructs components from the compiled file paths.
    tails = digest_tails(all_compiled)
    comps, usage = claude_from_files(cfg, tails)
    print(f"[claude] reconstructed {len(comps)} components from {len(tails)} file tails "
          f"(tok {usage.input_tokens}/{usage.output_tokens}):", flush=True)
    for c in comps:
        if c.name.strip().lower() in BUILD_TOOLS:
            print(f"    - {c.name}: dropped (build tool, not a shipped component)", flush=True)
            continue
        nk = norm_repo(c.vcs_url)
        new = nk and nk not in candidates
        print(f"    - {c.name} {c.proposed_version or '?'}  {c.vcs_url}  [{c.confidence}]"
              f"{'  <-- NEW (not in BD BoM)' if new else ''}", flush=True)
        if nk and new:      # (4) union: add repos BD missed, versions marked inferred
            candidates[nk] = {
                "name": c.name, "slug": c.name.lower().replace(" ", "-"),
                "vcs_url": c.vcs_url, "version": c.proposed_version,
                "version_source": "claude-inferred",
            }
    print(f"[union] {len(candidates)} candidate repos", flush=True)

    # (5) enumerate each candidate repo's file set at its tag.
    gh = GH(gh_token())
    filesets = {}
    for k, cand in candidates.items():
        owner, repo = parse_owner_repo(cand["vcs_url"])
        if not owner:
            continue
        tag = resolve_tag(gh, owner, repo, cand["version"])
        ref = tag or gh.get(f"/repos/{owner}/{repo}").get("default_branch", "master")
        try:
            fs, trunc = fetch_fileset(gh, owner, repo, ref)
        except Exception as exc:
            print(f"    ! {owner}/{repo}: tree fetch failed ({exc}); skipping", flush=True)
            continue
        cand["ref"] = ref
        cand["ref_is_tag"] = bool(tag)
        filesets[cand["slug"]] = fs
        print(f"    {cand['slug']:8} tree@{ref}: {len(fs)} files"
              f"{' (truncated)' if trunc else ''}", flush=True)

    # (6) attribute every compiled file to a repo (the mapping service).
    attribution = repo_mapper.attribute(sorted(all_compiled), filesets)
    prim_attr = {p: attribution.get(p) for p in primaries}

    # (7) monitored iff a PRIMARY TU landed on the repo.
    slug_by = {c["slug"]: c for c in candidates.values()}
    monitored = sorted({a.repo for a in prim_attr.values() if a})
    print(f"[classify] monitored (own >=1 primary TU): {monitored}", flush=True)

    # compiled-file index: only files attributed to a monitored repo.
    files, amb = [], 0
    for cp, a in sorted(attribution.items()):
        if a and a.repo in monitored:
            files.append({"path": f"{a.repo}/{a.rel}", "component": a.repo,
                          "kind": "header" if cp.lower().endswith(HDR_EXT) else "source",
                          "resolution": "mapper_longest_suffix",
                          **({"ambiguous": True} if a.ambiguous else {})})
            amb += a.ambiguous
    if amb:
        print(f"[classify] {amb} attributions were ambiguous (issue #4 punt)", flush=True)

    # write build-capture.json (monitored index)
    repos_detected = []
    for slug in monitored:
        c = slug_by[slug]
        repos_detected.append({
            "local_path": slug, "associated_component": slug,
            "pinned_ref": c.get("ref") or c.get("version"),
            "vcs_urls": [{"url": c["vcs_url"], "relationship": "upstream", "found_in": "bdcpp+mapper"}],
        })
    (args.dir / "build-capture.json").write_text(json.dumps({
        "_comment": ("Compiled-file index from a REAL BD/CPP capture, attributed to "
                     "repos by the mapping service (longest-suffix) over union(BD BoM, "
                     "Claude-from-files). Monitored = owns >=1 primary translation unit."),
        "project": args.project, "build_id": f"{args.project}@{args.version}",
        "repos_detected": repos_detected, "files": files,
    }, indent=2), encoding="utf-8")

    # rewrite hub-api-components.json as the union manifest with version_source
    items = []
    for c in candidates.values():
        items.append({
            "componentName": c["name"], "componentVersionName": c.get("version") or "?",
            "vcsUrl": c["vcs_url"], "versionSource": c["version_source"],
            "monitored_hint": c["slug"] in monitored,
        })
    (args.dir / "hub-api-components.json").write_text(json.dumps({
        "_comment": ("Union watch manifest: Black Duck BoM UNION Claude-from-compiled-"
                     "files. versionSource=bd is authoritative; claude-inferred is a best "
                     "estimate and the UI flags it."),
        "project": args.project, "version": args.version,
        "totalCount": len(items), "items": items,
    }, indent=2), encoding="utf-8")

    print(f"[write] build-capture.json: {len(files)} indexed files for {monitored}", flush=True)
    print(f"[write] hub-api-components.json: {len(items)} union repos", flush=True)
    ref_only = [c["slug"] for c in candidates.values() if c["slug"] not in monitored]
    print(f"[result] monitored={monitored}  reference-only={ref_only}", flush=True)


if __name__ == "__main__":
    main()
