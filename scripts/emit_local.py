"""Pure-stdlib local data collection for ingestion — the ONLY part that must run on
the build box (where the Coverity emit + the .git checkouts physically live).

Deliberately dependency-light: stdlib + the `git` binary only, no Anthropic / Black
Duck / requests imports. This lets a remote Linux/macOS build box collect everything
the ingestion API needs and POST it, without pulling the monitor's heavy dependency
tree onto the build machine. `attribute_capture` re-exports these so the server-side
pipeline keeps its existing import surface.

Two local artifacts, two collectors:
  parse_emit()             - the compiled file set from cov_emit_links.json
  collect_checkouts_local()- per-.git-checkout origin URL + exact ref built

The GitHub fork-parent resolution (checkout -> canonical upstream) is deliberately
NOT here: it needs a network call and is done server-side in ingest_service, so the
build box needs no GitHub token and the payload carries only local facts.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EMIT = Path(r"C:\Data\repo-monitoring-workspace\bdcpp-output\cov_emit_links.json")

SRC_EXT = (".c", ".cc", ".cpp", ".cxx")
HDR_EXT = (".h", ".hpp", ".hh", ".hxx")


def _is_scaffolding(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return "/cmakefiles/" in p or "/compilerid" in p


def parse_emit(emit_path: Path):
    """(primaries, all_compiled) from a Coverity cov_emit_links.json — the set of files
    actually compiled into this build (primary translation units + their inputs)."""
    d = json.loads(Path(emit_path).read_text(encoding="utf-8"))
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
    import re
    return re.sub(r"^https?://", "", (url or "").strip().lower()).rstrip("/").removesuffix(".git")


def slugify(name):
    return name.lower().replace(" ", "-")


def _git(root, *args):
    try:
        return subprocess.check_output(["git", "-C", root, *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def files_payload(primaries, all_compiled):
    """Shape the compiled file set for the ingestion payload: one row per file with
    is_primary (a primary translation unit?) and kind (source|header)."""
    prim = set(primaries)
    return [{"path": p, "is_primary": p in prim,
             "kind": "header" if p.lower().endswith(HDR_EXT) else "source"}
            for p in sorted(all_compiled)]


def collect_checkouts_local(paths):
    """For the compiled files, find each enclosing .git checkout's origin remote and the
    EXACT ref built (rev-parse HEAD + describe). Purely local — no network. Deduped by
    origin URL. The server resolves each origin to its canonical upstream (fork-parent).

    Returns: [{"actual_source_url": <origin>, "actual_source_ref": "<sha12> (<describe>)"}]
    """
    dir_cache, ref_cache = {}, {}

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
            sha = _git(root, "rev-parse", "HEAD")
            desc = _git(root, "describe", "--tags", "--always")
            ref_cache[root] = f"{(sha or '?')[:12]}" + (f" ({desc})" if desc and desc != sha else "")
        return ref_cache[root]

    out = {}   # norm(origin) -> record (first checkout wins for a given origin)
    for p in paths:
        root, url = root_remote(p)
        if not url:
            continue
        k = norm_repo(url)
        if k not in out:
            out[k] = {"actual_source_url": url, "actual_source_ref": exact_ref(root)}
    return list(out.values())
