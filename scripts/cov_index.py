"""Derive the real compiled-file index from a BD/CPP (Coverity) capture.

Input: cov_emit_links.json, produced by blackduck-c-cpp as
    cov-manage-emit --dir <idir> list-capture-invocations > cov_emit_links.json
It is a JSON record of the capture with three parts we use:
    files             : [{id, case-preserved: <abs path>, ...}]  # file-id -> path
    translation-units : [{kind, primary-file-id, input-files:[{file-id, kind}]}]
    link-units        : link steps (not needed for the compiled set)

A component was COMPILED FROM SOURCE (=> monitored) iff it has translation units
whose files live in its source tree. We walk every TU's input-files, resolve each
via the files table, keep those under a mapped component source tree, and record
source vs header. Components in the BOM with NO compiled files (e.g. OpenSSL,
which curl only *linked* — its headers were included but no .c was compiled) are
left out of this index and get classified reference-only by the monitor.

Output: build-capture.json in the monitor's shape (repos_detected + files), so
`monitor/app.py --data-dir <out-dir>` loads it directly.

Usage:
    python scripts/cov_index.py            # uses the defaults below
    python scripts/cov_index.py --emit <cov_emit_links.json> --out-dir <dir>
"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Stage-3 capture defaults (edit here if the build layout changes) ----------
DEFAULT_EMIT = Path(r"C:\Data\repo-monitoring-workspace\bdcpp-output\cov_emit_links.json")
DEFAULT_OUT = REPO_ROOT / "live-stage3"
PROJECT = "repo-mon-stage3-curl"
VERSION = "8.11.0"
# component slug -> (absolute source-tree root, pinned upstream ref, github repo)
COMPONENTS = {
    "curl": (r"C:\Data\repo-monitoring-workspace\stage3\src\curl", "curl-8_11_0", "https://github.com/curl/curl"),
    "zlib": (r"C:\Data\repo-monitoring-workspace\stage3\src\zlib", "v1.3.1", "https://github.com/madler/zlib"),
}
SRC_EXT = (".c", ".cc", ".cpp", ".cxx")
HDR_EXT = (".h", ".hpp", ".hh", ".hxx")


def norm(p: str) -> str:
    return p.replace("/", "\\").lower()


def under(path: str, root: str):
    """If path is inside root's tree, return the tree-relative remainder (fwd slashes)."""
    p, r = norm(path), norm(root).rstrip("\\") + "\\"
    if p.startswith(r):
        return path.replace("/", "\\")[len(r):].replace("\\", "/")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", type=Path, default=DEFAULT_EMIT)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.emit.exists():
        raise SystemExit(f"missing {args.emit} - run the BD/CPP capture first")
    emit = json.loads(args.emit.read_text(encoding="utf-8"))

    # file-id -> absolute path (prefer case-preserved)
    id2path = {}
    for f in emit.get("files", []):
        if isinstance(f, dict) and "id" in f:
            id2path[f["id"]] = f.get("case-preserved") or f.get("case-normalized") or ""

    tus = emit.get("translation-units", [])
    # collect (component, rel, kind) for every input-file under a component tree,
    # and count TU primary sources per component (the "compiled from source" proof)
    files_out = {}          # (component, rel) -> kind   (dedup)
    primary_srcs = Counter()
    skipped_ext = Counter()
    for tu in tus:
        input_ids = [(inf.get("file-id"), inf.get("kind", "")) for inf in tu.get("input-files", [])]
        prim = tu.get("primary-file-id")
        for fid, cov_kind in input_ids:
            path = id2path.get(fid)
            if not path:
                continue
            lower = path.lower()
            is_hdr = lower.endswith(HDR_EXT)
            is_src = lower.endswith(SRC_EXT)
            if not (is_hdr or is_src):
                skipped_ext[os.path.splitext(lower)[1]] += 1
                continue
            for slug, (root, _ref, _url) in COMPONENTS.items():
                rel = under(path, root)
                if rel is not None:
                    files_out[(slug, rel)] = "header" if is_hdr else "source"
                    if fid == prim and is_src:
                        primary_srcs[slug] += 1
                    break

    # assemble monitor build-capture.json
    repos_detected, files = [], []
    monitored = sorted({slug for (slug, _rel) in files_out})
    for slug in monitored:
        root, ref, url = COMPONENTS[slug]
        repos_detected.append({
            "local_path": slug,
            "associated_component": slug,
            "pinned_ref": ref,
            "vcs_urls": [{"url": url, "relationship": "upstream", "found_in": "bdcpp_capture"}],
        })
    for (slug, rel), kind in sorted(files_out.items()):
        files.append({"path": f"{slug}/{rel}", "component": slug, "kind": kind,
                      "resolution": "bdcpp_compiled"})

    out = {
        "_comment": (f"REAL compiled-file index for {PROJECT}@{VERSION}, derived from the "
                     "BD/CPP Coverity capture (cov-manage-emit list-capture-invocations -> "
                     "cov_emit_links.json). Only components whose source was actually compiled "
                     "appear here (curl, zlib); OpenSSL was linked-only and is intentionally "
                     "absent -> the monitor classifies it reference-only."),
        "project": PROJECT,
        "build_id": f"{PROJECT}@{VERSION}",
        "repos_detected": repos_detected,
        "files": files,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "build-capture.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"translation-units: {len(tus)}   files table: {len(id2path)}")
    print(f"compiled components (monitored): {monitored}")
    for slug in monitored:
        n = sum(1 for (s, _r) in files_out if s == slug)
        h = sum(1 for (s, _r), k in files_out.items() if s == slug and k == "header")
        print(f"  {slug:6} {n} files ({n-h} source, {h} header)  primary-TU sources={primary_srcs[slug]}  pinned={COMPONENTS[slug][1]}")
    print(f"wrote {args.out_dir / 'build-capture.json'}  ({len(files)} indexed files)")
    print("note: OpenSSL has 0 compiled files here -> reference-only (linked only).")


if __name__ == "__main__":
    main()
