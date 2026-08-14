"""Fetch REAL post-release commits (and a real compiled-file index) from the
upstream GitHub repos resolved by bd_provision, and save them as the demo's
replayable artifacts.

Two outputs, both written under live/ (gitignored):

  live/winscp-commit-events.json  - the N most recent commits on each watched
      repo's maintenance branch, in the same schema as samples/commit-events.json,
      so driver/replay.py fires them at the monitor with no further GitHub calls.

  live/build-capture.json  - a compiled-file index synthesized from each repo's
      source/header tree AT THE RELEASE TAG, in the shape monitor/app.py loads.
      This is an honest approximation: a real BD/CPP capture would narrow this to
      the translation units actually compiled into winscp.exe. We don't have the
      Embarcadero toolchain to run that capture, so we stand in the component's
      released source tree. Relevance filtering against it is therefore real, but
      broader than a true compiled set.

Why save instead of hitting GitHub every demo run: the commit data for an old
release tag is effectively static, and repeatedly pulling the same history from
GitHub for a demo is wasteful (and impolite to their servers). Fetch once here;
replay the saved files forever.

GitHub auth uses your existing `gh` login (via `gh auth token`) or GITHUB_TOKEN.

Usage:
    python scripts/gh_replay.py                 # fetch commits + index for all repos
    python scripts/gh_replay.py --commits 8     # N recent commits per repo (default 10)
    python scripts/gh_replay.py --events-only    # skip the compiled-file index
"""

import argparse
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_scout import load_config  # noqa: E402  (only for consistency / future use)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "live"
MANIFEST = LIVE_DIR / "hub-api-components.json"

GH_API = "https://api.github.com"
SRC_EXT = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx")
HDR_EXT = (".h", ".hpp", ".hh", ".hxx")
MAX_INDEX_FILES = 800  # cap per component so build-capture.json stays readable


def gh_token() -> str:
    import os
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        sys.exit("no GitHub token: set GITHUB_TOKEN or run `gh auth login`")


class GH:
    def __init__(self, token: str):
        self.token = token
        self.ctx = ssl.create_default_context()

    def get(self, path: str):
        url = path if path.startswith("http") else f"{GH_API}{path}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RepoMonitoring-demo",
        })
        with urllib.request.urlopen(req, timeout=30, context=self.ctx) as resp:
            return json.loads(resp.read())

    def exists(self, path: str) -> bool:
        try:
            self.get(path)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise


def parse_owner_repo(vcs_url: str):
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", vcs_url.strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def resolve_tag(gh: GH, owner: str, repo: str, version: str):
    v = version.strip()
    u = v.replace(".", "_")
    for cand in (f"v{v}", v, f"R_{u}", f"OpenSSL_{u}", f"rel-{v}", f"release-{v}", u):
        if gh.exists(f"/repos/{owner}/{repo}/git/ref/tags/{cand}"):
            return cand
    return None


def resolve_watch_branch(gh: GH, owner: str, repo: str, version: str, fallback: str):
    """Prefer the maintenance branch nearest the built version (the design's
    'fixes land on the stable branch first' story), else the resolver's branch,
    else the repo default."""
    nums = re.findall(r"\d+", version)
    cands = []
    if len(nums) >= 3:
        a, b, c = nums[0], nums[1], nums[2]
        cands += [f"OpenSSL_{a}_{b}_{c}-stable", f"{a}.{b}-stable", f"{a}.{b}.x",
                  f"stable/{a}.{b}", f"linux-{a}.{b}.y"]
    if len(nums) >= 2:
        a, b = nums[0], nums[1]
        cands += [f"{a}.{b}-stable", f"{a}.{b}.x"]
    for br in cands:
        if gh.exists(f"/repos/{owner}/{repo}/branches/{br}"):
            return br, "maintenance branch nearest built version"
    if fallback and gh.exists(f"/repos/{owner}/{repo}/branches/{fallback}"):
        return fallback, "resolver-provided default branch"
    default = gh.get(f"/repos/{owner}/{repo}").get("default_branch", "master")
    return default, "repo default branch"


def fetch_commits(gh: GH, owner: str, repo: str, branch: str, n: int, slug: str):
    listing = gh.get(f"/repos/{owner}/{repo}/commits?sha={branch}&per_page={n}")
    events = []
    for i, item in enumerate(listing, 1):
        sha = item["sha"]
        detail = gh.get(f"/repos/{owner}/{repo}/commits/{sha}")
        added, modified, removed = [], [], []
        for f in detail.get("files", []):
            path, status = f.get("filename"), f.get("status")
            if status == "added":
                added.append(path)
            elif status == "removed":
                removed.append(path)
            else:  # modified, renamed, changed, copied
                modified.append(path)
        msg = (item.get("commit", {}).get("message", "") or "").splitlines()[0][:100]
        events.append({
            "id": f"{slug}-{i:02d}",
            "scenario": f"Live upstream commit on {owner}/{repo}@{branch}: {msg}",
            "vcs_url": f"https://github.com/{owner}/{repo}",
            "branch": branch,
            "commit": sha,
            "message": msg,
            "committed_at": item.get("commit", {}).get("committer", {}).get("date", ""),
            "files_changed": added + modified + removed,
        })
    return events


def fetch_index(gh: GH, owner: str, repo: str, tag: str, slug: str):
    tree = gh.get(f"/repos/{owner}/{repo}/git/trees/{tag}?recursive=1")
    files, truncated = [], tree.get("truncated", False)
    blobs = [b["path"] for b in tree.get("tree", [])
             if b.get("type") == "blob" and b["path"].lower().endswith(SRC_EXT)]
    blobs.sort()
    if len(blobs) > MAX_INDEX_FILES:
        # deterministic thinning: keep an even sample across the sorted tree
        step = len(blobs) / MAX_INDEX_FILES
        blobs = [blobs[int(i * step)] for i in range(MAX_INDEX_FILES)]
        truncated = True
    for p in blobs:
        files.append({
            "path": f"{slug}/{p}",
            "component": slug,
            "kind": "header" if p.lower().endswith(HDR_EXT) else "source",
            "resolution": "gh_tree_approx",
        })
    return files, truncated


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commits", type=int, default=10, help="recent commits per repo")
    ap.add_argument("--events-only", action="store_true",
                    help="skip the gh_tree compiled-file index (use with a real BD/CPP capture)")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--out-dir", type=Path, default=LIVE_DIR, help="where to write the events/index")
    ap.add_argument("--events-name", default="winscp-commit-events.json",
                    help="filename for the saved events")
    args = ap.parse_args()
    out_dir = args.out_dir

    if not args.manifest.exists():
        sys.exit(f"missing {args.manifest} - run scripts/bd_provision.py first")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))

    gh = GH(gh_token())
    all_events, all_files, repos_detected = [], [], []

    for item in manifest.get("items", []):
        name, version = item["componentName"], item["componentVersionName"]
        slug = name.lower().replace(" ", "-")
        owner, repo = parse_owner_repo(item.get("vcsUrl") or "")
        if not owner:
            print(f"[skip] {name} {version}: no GitHub repo (vcsUrl={item.get('vcsUrl')})", flush=True)
            continue

        print(f"[{owner}/{repo}] {name} {version}", flush=True)
        try:
            tag = resolve_tag(gh, owner, repo, version)
            branch, why = resolve_watch_branch(gh, owner, repo, version, item.get("vcsBranch") or "")
            print(f"    tag={tag or '(unresolved)'}  watch-branch={branch}  ({why})", flush=True)

            events = fetch_commits(gh, owner, repo, branch, args.commits, slug)
            all_events.extend(events)
            print(f"    fetched {len(events)} commits", flush=True)

            if not args.events_only and tag:
                files, trunc = fetch_index(gh, owner, repo, tag, slug)
                all_files.extend(files)
                repos_detected.append({
                    "local_path": slug,
                    "associated_component": slug,
                    "pinned_ref": tag,
                    "vcs_urls": [{"url": f"https://github.com/{owner}/{repo}",
                                  "relationship": "upstream", "found_in": "gh_tree"}],
                })
                print(f"    indexed {len(files)} source/header files at {tag}"
                      f"{' (sampled)' if trunc else ''}", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"    ! GitHub HTTP {exc.code}: {exc.reason} - skipping", flush=True)
            continue

    out_dir.mkdir(parents=True, exist_ok=True)

    events_out = out_dir / args.events_name
    events_out.write_text(json.dumps({
        "_comment": (f"REAL upstream commits fetched from GitHub for {manifest.get('project','?')}, "
                     "saved so the demo replays without hitting GitHub. Schema matches "
                     "samples/commit-events.json; fire with driver/replay.py "
                     f"--events {events_out.name}."),
        "events": all_events,
    }, indent=2), encoding="utf-8")
    print(f"\n[write] {len(all_events)} events -> {events_out}", flush=True)

    if not args.events_only:
        capture_out = out_dir / "build-capture.json"
        capture_out.write_text(json.dumps({
            "_comment": ("Compiled-file index APPROXIMATED from each component's "
                         "released source tree on GitHub (kind=gh_tree_approx). A real "
                         "BD/CPP capture would narrow this to actually-compiled TUs; "
                         "the Embarcadero build needed for that is deferred."),
            "project": manifest.get("project", "winscp.exe"),
            "build_id": f"{manifest.get('project','winscp.exe')}@{manifest.get('version','1')}-gh-approx",
            "repos_detected": repos_detected,
            "files": all_files,
        }, indent=2), encoding="utf-8")
        print(f"[write] {len(all_files)} indexed files -> {capture_out}", flush=True)

    print("\nNext:", flush=True)
    print(f"  python monitor/app.py --data-dir {out_dir}", flush=True)
    print(f"  python driver/replay.py --events {events_out} --all", flush=True)


if __name__ == "__main__":
    main()
