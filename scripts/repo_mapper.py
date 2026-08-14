"""Compiled-file -> repo attribution service (the mapping seam).

This is deliberately a single fat batch call: hand it EVERY compiled file path
plus EVERY candidate repo's file set, get back the full path->repo mapping. In a
pre-LLM world that shape looks like an anti-pattern, but it is exactly what a
Software Composition Analysis matcher does, and keeping it as one call means a
future implementation can pass the entire data set to a frontier model in one
context window instead of the fixed rule below.

    attribute(compiled_paths, repo_filesets) -> {compiled_path: Attribution|None}

Current implementation: LONGEST-SUFFIX match. A compiled path is attributed to
the repo whose file set contains the longest path-suffix of that compiled path.
It needs no knowledge of where each repo's source root sits on disk — the repo
file sets are the dictionary, the compiled path's tail is the query.

Deliberately punted (issue #4), and this is where it will be fixed: when the same
relative path exists in more than one candidate repo — a local vendor FORK and
its UPSTREAM, or a project that vendors a copy of a dependency — longest-suffix
alone cannot tell them apart. Today we break the tie deterministically and move
on; that is the first thing to make correct, because building from source usually
means vendor edits (a fork), so this case is common, not rare.
"""

from typing import Dict, List, NamedTuple, Optional, Set


class Attribution(NamedTuple):
    repo: str          # candidate repo slug the file was attributed to
    rel: str           # repo-relative path that matched (the suffix)
    depth: int         # number of trailing path segments matched (match strength)
    ambiguous: bool    # True if the winning suffix existed in >1 repo (see issue #4)


def _segs(path: str) -> List[str]:
    return [s for s in path.replace("\\", "/").lower().split("/") if s]


def attribute(compiled_paths: List[str],
              repo_filesets: Dict[str, Set[str]]) -> Dict[str, Optional[Attribution]]:
    """Map each compiled file path to the candidate repo it most likely came from.

    compiled_paths : absolute (or any) compiled file paths from the build capture.
    repo_filesets  : repo_slug -> set of repo-relative file paths (the repo's tree
                     at the pinned tag).
    returns        : compiled_path -> Attribution (or None if nothing matched).
    """
    # Index every repo file by its reversed segment tuple -> {repos owning it}.
    # (case-folded via _segs so paths from the two sides compare cleanly).
    index: Dict[tuple, Set[str]] = {}
    rel_of: Dict[tuple, str] = {}   # reversed-tuple -> a canonical forward-slash rel
    for repo, files in repo_filesets.items():
        for f in files:
            segs = _segs(f)
            if not segs:
                continue
            key = tuple(reversed(segs))
            index.setdefault(key, set()).add(repo)
            rel_of.setdefault(key, "/".join(segs))

    out: Dict[str, Optional[Attribution]] = {}
    for cp in compiled_paths:
        csegs = list(reversed(_segs(cp)))
        hit: Optional[Attribution] = None
        for n in range(len(csegs), 0, -1):      # longest suffix first
            key = tuple(csegs[:n])
            repos = index.get(key)
            if repos:
                # tie-break (#4 punt): deterministic pick; flag ambiguity.
                repo = sorted(repos)[0]
                hit = Attribution(repo=repo, rel=rel_of[key], depth=n,
                                  ambiguous=len(repos) > 1)
                break
        out[cp] = hit
    return out


# --------------------------------------------------------------- self-test
if __name__ == "__main__":
    compiled = [
        r"C:\build\stage3\src\curl\lib\multi.c",          # -> curl (lib/multi.c)
        r"C:\build\stage3\src\curl\lib\vtls\openssl.c",   # -> curl (curl's own tls glue)
        r"C:\build\stage3\src\zlib\inflate.c",            # -> zlib
        r"C:\build\stage3\openssl-install\include\openssl\ssl.h",  # -> openssl (a HEADER)
        r"C:\build\weird\layout\deeply\nested\gzread.c",  # -> zlib by basename only (depth 1)
        r"C:\build\stage3\src\curl\lib\config.h",         # ambiguous: curl AND zlib both have config.h
        r"C:\build\stage3\src\curl\CMakeLists.txt",       # no source match -> None
    ]
    filesets = {
        "curl": {"lib/multi.c", "lib/vtls/openssl.c", "lib/config.h", "src/tool_main.c"},
        "zlib": {"inflate.c", "gzread.c", "config.h"},
        "openssl": {"include/openssl/ssl.h", "crypto/mem.c"},
    }
    res = attribute(compiled, filesets)
    for cp in compiled:
        a = res[cp]
        tag = f"{a.repo:8} rel={a.rel:20} depth={a.depth}{' AMBIGUOUS' if a.ambiguous else ''}" if a else "(no match)"
        print(f"  {cp.split(chr(92))[-1]:16} -> {tag}")
