"""Black Duck SCA discovery helper for the RepoMonitoring demo.

Authenticates against the test instance defined in blackduck.local.json and
summarizes projects by BOM size, so we can pick a good demo target: a project
version with 3-7 components whose components map to active GitHub repos.

Reads credentials from blackduck.local.json (gitignored). The API token never
appears on a command line or in output; only project/component names and counts
are printed.

Usage:
    python scripts/bd_scout.py                 # list projects + version BOM counts
    python scripts/bd_scout.py --project NAME  # dump the BOM of a project's versions
    python scripts/bd_scout.py --min 3 --max 7 # filter to candidate BOM sizes
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "blackduck.local.json"

USER_MEDIA = "application/vnd.blackducksoftware.user-4+json"
BOM_MEDIA = "application/vnd.blackducksoftware.bill-of-materials-6+json"
JSON_MEDIA = "application/json"


def load_config() -> dict:
    """Config from blackduck.local.json, or the file named by BLACKDUCK_LOCAL_CONFIG —
    so a run can target a second instance (e.g. blackduck.local.sca233.json) without
    swapping the default file under other live sessions."""
    path = Path(os.environ["BLACKDUCK_LOCAL_CONFIG"]) if os.environ.get(
        "BLACKDUCK_LOCAL_CONFIG") else CONFIG_PATH
    if not path.exists():
        sys.exit(f"missing config: {path} (copy blackduck.local.example.json)")
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if "your-instance" in cfg.get("url", "") or "PASTE" in cfg.get("api_token", ""):
        sys.exit("blackduck.local.json still has placeholder values — fill in url + api_token")
    cfg["url"] = cfg["url"].rstrip("/")
    return cfg


class BDClient:
    def __init__(self, base_url: str, api_token: str, insecure: bool = False):
        self.base = base_url.rstrip("/")
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.bearer = self._authenticate(api_token)

    def _authenticate(self, api_token: str) -> str:
        req = urllib.request.Request(
            f"{self.base}/api/tokens/authenticate",
            data=b"",
            headers={"Authorization": f"token {api_token}", "Accept": USER_MEDIA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            sys.exit(f"auth failed: HTTP {exc.code} {exc.reason}\n{body}")
        except urllib.error.URLError as exc:
            sys.exit(f"auth failed: cannot reach {self.base} ({exc.reason})")
        token = data.get("bearerToken")
        if not token:
            sys.exit(f"auth response had no bearerToken: {data}")
        return token

    def get(self, url_or_path: str, media: str = JSON_MEDIA) -> dict:
        url = url_or_path if url_or_path.startswith("http") else f"{self.base}{url_or_path}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.bearer}", "Accept": media},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30, context=self.ctx) as resp:
            return json.loads(resp.read())

    def get_all(self, url_or_path: str, media: str = JSON_MEDIA, page: int = 100) -> list:
        """Follow offset pagination, returning the flattened items list."""
        items, offset = [], 0
        sep = "&" if "?" in url_or_path else "?"
        while True:
            batch = self.get(f"{url_or_path}{sep}limit={page}&offset={offset}", media)
            got = batch.get("items", [])
            items.extend(got)
            total = batch.get("totalCount", len(items))
            offset += page
            if offset >= total or not got:
                break
        return items


def meta_href(obj: dict) -> str:
    return (obj.get("_meta") or {}).get("href", "")


def count_components(client: BDClient, version_href: str) -> int:
    """Cheap component count: fetch one row and read totalCount (no full page)."""
    batch = client.get(f"{version_href}/components?limit=1", BOM_MEDIA)
    return batch.get("totalCount", 0)


def scout(client: BDClient, min_c: int, max_c: int, limit_projects: int) -> None:
    # Most-recently-updated projects first, so demo-relevant ones surface early.
    projects = client.get_all("/api/projects?sort=updatedAt%20DESC")
    total_projects = len(projects)
    if limit_projects and total_projects > limit_projects:
        print(f"{total_projects} projects; scanning most-recent {limit_projects} "
              f"(raise with --limit-projects)\n", flush=True)
        projects = projects[:limit_projects]
    else:
        print(f"{total_projects} projects on {client.base}\n", flush=True)

    candidates = []
    for i, p in enumerate(projects, 1):
        name = p.get("name", "?")
        try:
            versions = client.get_all(f"{meta_href(p)}/versions")
        except urllib.error.HTTPError as exc:
            print(f"[{i}/{len(projects)}] ! {name}: versions HTTP {exc.code}", flush=True)
            continue
        for v in versions:
            vname = v.get("versionName", "?")
            try:
                n = count_components(client, meta_href(v))
            except urllib.error.HTTPError as exc:
                print(f"[{i}/{len(projects)}]  ! {name} @ {vname}: components HTTP {exc.code}",
                      flush=True)
                continue
            flag = "  <-- candidate" if min_c <= n <= max_c else ""
            print(f"[{i}/{len(projects)}]  {name} @ {vname}: {n} components{flag}", flush=True)
            if flag:
                candidates.append((name, vname, meta_href(v)))

    print(flush=True)
    if not candidates:
        print(f"No project version had {min_c}-{max_c} components. Widen --min/--max.")
        return
    print(f"=== {len(candidates)} candidate version(s) — dumping BOMs ===", flush=True)
    for name, vname, href in candidates:
        print(f"\n# {name} @ {vname}\n  {href}", flush=True)
        for c in client.get_all(f"{href}/components", BOM_MEDIA):
            origins = ", ".join(o.get("externalId", "") for o in c.get("origins", []))
            print(f"    - {c.get('componentName','?')} {c.get('componentVersionName','?')}"
                  f"   origins:[{origins}]  vcsUrl:{c.get('vcsUrl','(none)')}", flush=True)


def dump_project(client: BDClient, project_name: str) -> None:
    projects = client.get_all("/api/projects")
    match = [p for p in projects if p.get("name", "").lower() == project_name.lower()]
    if not match:
        match = [p for p in projects if project_name.lower() in p.get("name", "").lower()]
    if not match:
        sys.exit(f"no project matching {project_name!r}")
    for p in match:
        print(f"# project: {p.get('name')}")
        for v in client.get_all(f"{meta_href(p)}/versions"):
            comps = client.get_all(f"{meta_href(v)}/components", BOM_MEDIA)
            print(f"  version {v.get('versionName')}: {len(comps)} components  {meta_href(v)}")
            for c in comps:
                print("    " + json.dumps({
                    "componentName": c.get("componentName"),
                    "componentVersionName": c.get("componentVersionName"),
                    "origins": [o.get("externalId") for o in c.get("origins", [])],
                    "vcsUrl": c.get("vcsUrl"),
                }))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="dump BOM for a specific project (name or substring)")
    ap.add_argument("--min", type=int, default=3, help="candidate min component count")
    ap.add_argument("--max", type=int, default=7, help="candidate max component count")
    ap.add_argument("--limit-projects", type=int, default=60,
                    help="scan at most N most-recently-updated projects (0 = all)")
    ap.add_argument("--list-only", action="store_true",
                    help="just list project names + count, no BOM probing")
    args = ap.parse_args()

    # Windows consoles default to cp1252; project names carry Unicode. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = load_config()
    client = BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False))
    print(f"[bd_scout] authenticated to {client.base}\n", flush=True)

    if args.list_only:
        projects = client.get_all("/api/projects?sort=updatedAt%20DESC")
        print(f"{len(projects)} projects:\n", flush=True)
        for p in projects:
            print(f"  {p.get('name','?')}", flush=True)
    elif args.project:
        dump_project(client, args.project)
    else:
        scout(client, args.min, args.max, args.limit_projects)


if __name__ == "__main__":
    main()
