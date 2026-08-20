"""Compiled-file -> repo attribution service (the mapping seam).

Fat batch call: hand it EVERY compiled file path plus EVERY candidate repo's
file set, get back — per compiled path — the FULL LIST of repos it belongs to.
Keeping it a single call means a future implementation can hand the entire data
set to a frontier model in one context window instead of the rule below.

    attribute(compiled_paths, repo_filesets) -> {compiled_path: [Attribution, ...]}

A file can legitimately belong to MORE THAN ONE repo. The motivating case: a
project that carries an inline-vendored copy of a dependency —
`third_party/zlib/inflate.c` is both the host repo's file (provenance) AND
genuine upstream zlib whose security fixes land in canonical `madler/zlib`. Both
must be watched, so the mapping is one-to-many, not one-to-one. (A file you both
build directly and vendor indirectly is the same story.)

Two signals build the list:

  * longest-suffix -> the PRIMARY owner(s): the repo whose file set contains the
    longest path-suffix of the compiled path. Ties at the winning depth list all
    of them (an honest "same path in two repos").
  * vendored cluster -> when a whole DIRECTORY of compiled files collectively
    matches a large fraction of another repo's tree, that repo is a vendored-copy
    source and is added as a secondary attribution for those files. The
    directory-level test is what separates real inline vendoring (a subtree that
    mirrors zlib) from a lone coincidental shared basename (`util.c`), which must
    NOT fan out. Tune via VENDOR_MIN_FILES / VENDOR_MIN_FRAC below.

This is the seam where a fork/vendored disambiguation smarter than longest-suffix
(up to an LLM over the whole set) would slot in; the batch interface stays fixed.
"""

from collections import defaultdict
from typing import Dict, List, NamedTuple, Set

# A directory is treated as a vendored copy of repo R when at least this many of
# its compiled files match R's tree AND they are at least this fraction of the
# directory. Tuned to fire on a real vendored subtree (many matching files) while
# staying silent on incidental basename collisions (one or two files).
VENDOR_MIN_FILES = 3
VENDOR_MIN_FRAC = 0.5


class Attribution(NamedTuple):
    repo: str          # candidate repo slug the file is attributed to
    rel: str           # that repo's real relative path for this file
    depth: int         # trailing path segments matched (match strength)
    kind: str          # "primary" (longest-suffix owner) | "vendored" (cluster)


def _segs(path: str) -> List[str]:
    return [s for s in path.replace("\\", "/").lower().split("/") if s]


def _parent_dir(path: str) -> str:
    return "/".join(_segs(path)[:-1])


def attribute(compiled_paths: List[str],
              repo_filesets: Dict[str, Set[str]]) -> Dict[str, List[Attribution]]:
    """Map each compiled file path to the LIST of candidate repos it belongs to.

    compiled_paths : absolute (or any) compiled file paths from the build capture.
    repo_filesets  : repo_slug -> set of repo-relative file paths (the repo's tree
                     at the pinned ref).
    returns        : compiled_path -> [Attribution, ...] (empty list = no match).
                     The primary owner(s) come first, vendored secondaries after.
    """
    # reversed full-path key -> {repo: that repo's forward-slash real rel}.
    # (case-folded via _segs so paths from the two sides compare cleanly.)
    index: Dict[tuple, Dict[str, str]] = {}
    for repo, files in repo_filesets.items():
        for f in files:
            segs = _segs(f)
            if not segs:
                continue
            index.setdefault(tuple(reversed(segs)), {})[repo] = "/".join(segs)

    # Per path: repo -> (best_depth, rel), the deepest suffix match in each repo.
    hits: Dict[str, Dict[str, tuple]] = {}
    for cp in compiled_paths:
        csegs = list(reversed(_segs(cp)))
        h: Dict[str, tuple] = {}
        for n in range(1, len(csegs) + 1):          # every suffix depth, shallow->deep
            m = index.get(tuple(csegs[:n]))
            if not m:
                continue
            for repo, rel in m.items():
                if repo not in h or n > h[repo][0]:
                    h[repo] = (n, rel)
        hits[cp] = h

    # Vendored-cluster licensing: for each directory, which repos does a large
    # fraction of its files collectively match? Those are vendored-copy sources.
    dir_total: Dict[str, int] = defaultdict(int)
    dir_repo: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for cp in compiled_paths:
        d = _parent_dir(cp)
        dir_total[d] += 1
        for repo in hits[cp]:
            dir_repo[d][repo] += 1
    licensed: Dict[str, Set[str]] = {}
    for d, total in dir_total.items():
        licensed[d] = {repo for repo, cnt in dir_repo[d].items()
                       if cnt >= VENDOR_MIN_FILES and cnt / total >= VENDOR_MIN_FRAC}

    out: Dict[str, List[Attribution]] = {}
    for cp in compiled_paths:
        h = hits[cp]
        if not h:
            out[cp] = []
            continue
        maxdepth = max(d for d, _ in h.values())
        primaries = {r for r, (d, _) in h.items() if d == maxdepth}
        attrs = [Attribution(r, h[r][1], h[r][0], "primary") for r in sorted(primaries)]
        # Add cluster-licensed repos that aren't already the primary owner: these
        # are the vendored-copy sources a low-depth (basename) match alone would
        # never justify without the directory-level evidence.
        for r in sorted(licensed.get(_parent_dir(cp), ())):
            if r in primaries:
                continue
            # `licensed` is a DIRECTORY-level verdict, so it can name a repo that
            # matched sibling files but not this one; there is no rel path to
            # attribute in that case. (Guard, not a filter: h[r] used to raise.)
            hr = h.get(r)
            if hr is None:
                continue
            depth, rel = hr
            attrs.append(Attribution(r, rel, depth, "vendored"))
        out[cp] = attrs
    return out


# --------------------------------------------------------------- self-test
if __name__ == "__main__":
    # curl VENDORS a copy of zlib under deps/zlib/ (committed in curl's own tree),
    # and there is ALSO a standalone zlib checkout. openssl is headers-only.
    filesets = {
        "curl": {"lib/multi.c", "lib/vtls/openssl.c", "lib/config.h", "src/tool_main.c",
                 "src/util.c",
                 "deps/zlib/inflate.c", "deps/zlib/deflate.c", "deps/zlib/zutil.c",
                 "deps/zlib/gzread.c"},
        "zlib": {"inflate.c", "deflate.c", "zutil.c", "gzread.c", "config.h", "util.c"},
        "openssl": {"include/openssl/ssl.h", "crypto/mem.c"},
    }
    compiled = [
        r"C:\build\curl\lib\multi.c",              # curl only
        r"C:\build\curl\lib\vtls\openssl.c",       # curl only  (NOT openssl — regression)
        r"C:\build\curl\src\util.c",               # curl only  (util.c also in zlib, but LONE -> no fanout)
        r"C:\build\curl\deps\zlib\inflate.c",      # curl + zlib (vendored cluster)
        r"C:\build\curl\deps\zlib\deflate.c",      # curl + zlib
        r"C:\build\curl\deps\zlib\zutil.c",        # curl + zlib
        r"C:\build\curl\deps\zlib\gzread.c",       # curl + zlib
        r"C:\build\zlib\inflate.c",                # zlib only (standalone checkout)
        r"C:\build\openssl\include\openssl\ssl.h", # openssl only (a header)
        r"C:\build\curl\CMakeLists.txt",           # nothing matches -> []
    ]
    res = attribute(compiled, filesets)

    def repos(cp):
        return {a.repo for a in res[cp]}

    # ---- assertions (the plan's regression + dual-map guarantees) ----
    assert repos(compiled[0]) == {"curl"}, res[compiled[0]]
    assert repos(compiled[1]) == {"curl"}, ("vtls/openssl.c must stay curl", res[compiled[1]])
    assert repos(compiled[2]) == {"curl"}, ("lone util.c collision must NOT fan out", res[compiled[2]])
    for cp in compiled[3:7]:
        assert repos(cp) == {"curl", "zlib"}, ("vendored subtree must dual-map", cp, res[cp])
        kinds = {a.repo: a.kind for a in res[cp]}
        assert kinds["curl"] == "primary" and kinds["zlib"] == "vendored", (cp, kinds)
    assert repos(compiled[7]) == {"zlib"}, res[compiled[7]]
    assert repos(compiled[8]) == {"openssl"}, res[compiled[8]]
    assert res[compiled[9]] == [], res[compiled[9]]

    print("repo_mapper self-test: PASS\n")
    for cp in compiled:
        tag = ", ".join(f"{a.repo}:{a.rel}(d{a.depth},{a.kind})" for a in res[cp]) or "(no match)"
        print(f"  {cp.split(chr(92))[-1]:16} -> {tag}")
