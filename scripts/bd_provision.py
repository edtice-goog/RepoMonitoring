"""Live provisioning for the RepoMonitoring demo.

Pulls a real Black Duck SCA BOM for the configured project version, then
enhances each component with an upstream VCS (GitHub) URL via a Claude API
call — because the Hub BOM endpoint does not itself serve a per-component
vcsUrl today. The result is written in the same shape the monitor already
consumes (samples/hub-api-components.json), so the watch manifest becomes
genuinely Black-Duck-derived instead of hand-crafted.

    live Black Duck BOM  ->  Claude VCS enhancement  ->  watch manifest

Honest framing for the demo: in a real product the VCS URL would be resolved
once by the Black Duck KB and stored, not recomputed by an LLM on every run
(wasteful, and non-deterministic). Doing it here with our own Claude call
demonstrates the *potential* end to end.

Config comes from blackduck.local.json (gitignored): url, api_token,
anthropic_api_key, project, version. Output goes to live/ (gitignored).

Usage:
    python scripts/bd_provision.py                 # provision configured project@version
    python scripts/bd_provision.py --project X --version Y
    python scripts/bd_provision.py --dry-run       # fetch BOM, skip the Claude call
"""

import argparse
import json
import sys
import urllib.error
from pathlib import Path
from typing import List, Literal, Optional

# Reuse the authenticated Hub client + config loader from the scout helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_scout import BDClient, load_config, meta_href, BOM_MEDIA  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "live"
# Demo default is a fast, economical model — there is no value in paying for a
# frontier model to resolve repos for illustrative sample data. A real product
# deployment would pass --model claude-opus-5 (or newer).
MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """\
You enhance a software Bill of Materials (SBOM) with upstream source-repository \
URLs, for a defensive patch-gap monitoring tool. For each open-source component \
you are given its name, version, and any origin identifiers a Software \
Composition Analysis tool recorded (e.g. "madler/zlib:v1.3.1", \
"libexpat/libexpat:R_2_5_0", or a distro package id).

For each component, return the canonical **upstream development repository** — \
where security fixes are committed first — as an https clone URL with no \
trailing ".git" (e.g. https://github.com/madler/zlib, \
https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux). Prefer the real \
upstream over a distro mirror or a release-tarball host. If an origin id already \
encodes a GitHub "owner/repo", trust it. Also give the repository's default \
branch when you know it (e.g. "main", "master", "develop").

Set confidence honestly:
- "high": you are confident of the exact upstream repo.
- "medium": likely correct but the component name/origin is ambiguous.
- "low": a plausible guess only.
- "none": no upstream repo is known or the component is not open source; set \
vcs_url to null.

Keep rationale to one short factual sentence. Do not invent repositories."""


def build_anthropic_client(api_key: str):
    try:
        import anthropic
    except ImportError:
        sys.exit(
            "The 'anthropic' package is required for live provisioning.\n"
            "  pip install -r requirements-live.txt\n"
            "(The offline demo needs no dependencies; only this live step does.)"
        )
    return anthropic.Anthropic(api_key=api_key)


def resolve_project_version(client: BDClient, project_name: str, version_name: str):
    """Return the components-endpoint items for one project@version."""
    projects = client.get_all("/api/projects")
    proj = next((p for p in projects if p.get("name", "").lower() == project_name.lower()), None)
    if proj is None:
        proj = next((p for p in projects if project_name.lower() in p.get("name", "").lower()), None)
    if proj is None:
        sys.exit(f"no project matching {project_name!r} on {client.base}")

    versions = client.get_all(f"{meta_href(proj)}/versions")
    ver = next((v for v in versions if v.get("versionName", "").lower() == version_name.lower()), None)
    if ver is None:
        avail = ", ".join(v.get("versionName", "?") for v in versions)
        sys.exit(f"no version {version_name!r} for project {proj.get('name')!r}. Available: {avail}")

    comps = client.get_all(f"{meta_href(ver)}/components", BOM_MEDIA)
    return proj, ver, comps


def bd_ui_url(base_url: str, proj: dict, ver: dict) -> str:
    """The human-facing Black Duck web-UI link for one project version (the API
    hrefs from _meta are not clickable for a person)."""
    pid = meta_href(proj).rstrip("/").rsplit("/", 1)[-1]
    vid = meta_href(ver).rstrip("/").rsplit("/", 1)[-1]
    return f"{base_url.rstrip('/')}/projects/{pid}/versions/{vid}/components"


def component_context(comps: list) -> list:
    """Distill each BOM component to the fields Claude needs to resolve a repo."""
    out = []
    for c in comps:
        origins = [
            {"namespace": o.get("externalNamespace"), "externalId": o.get("externalId")}
            for o in c.get("origins", [])
        ]
        out.append({
            "componentName": c.get("componentName", "?"),
            "componentVersionName": c.get("componentVersionName", "?"),
            "origins": origins,
            "licenses": [l.get("licenseDisplay") for l in c.get("licenses", [])],
        })
    return out


def enhance_with_claude(anthropic_client, components: list):
    """One Claude call resolves upstream repos for every component."""
    from pydantic import BaseModel

    class ResolvedRepo(BaseModel):
        component_name: str
        component_version: str
        vcs_url: Optional[str]
        default_branch: Optional[str]
        confidence: Literal["high", "medium", "low", "none"]
        rationale: str

    class Resolution(BaseModel):
        repos: List[ResolvedRepo]

    payload = {
        "instruction": "Resolve the upstream source repository for each component below.",
        "components": components,
    }
    resp = anthropic_client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        output_format=Resolution,
    )
    return resp.parsed_output.repos, resp.usage


def to_watch_manifest(project, version, comps, resolved) -> dict:
    """Assemble the hub-api-components.json shape the monitor loads, now with
    live componentName/componentVersionName and Claude-resolved vcsUrl."""
    by_key = {(r.component_name.lower(), r.component_version.lower()): r for r in resolved}
    items = []
    for c in comps:
        name = c.get("componentName", "?")
        ver = c.get("componentVersionName", "?")
        r = by_key.get((name.lower(), ver.lower()))
        items.append({
            "componentName": name,
            "componentVersionName": ver,
            "matchTypes": c.get("matchTypes", []),
            "usages": c.get("usages", []),
            "origins": [{"externalNamespace": o.get("externalNamespace"),
                         "externalId": o.get("externalId")} for o in c.get("origins", [])],
            "licenses": [{"licenseDisplay": l.get("licenseDisplay")} for l in c.get("licenses", [])],
            # Enhanced fields — sourced from Claude, not the Hub BOM:
            "vcsUrl": (r.vcs_url if r else None),
            "vcsBranch": (r.default_branch if r else None),
            "vcsConfidence": (r.confidence if r else "none"),
            "vcsRationale": (r.rationale if r else "no resolution returned"),
            "vcsProvenance": "claude:vcs_enhancement",
        })
    return {
        "_comment": (
            "LIVE artifact: components pulled from the Black Duck SCA BOM for "
            f"{project.get('name')}@{version.get('versionName')}; vcsUrl fields "
            "enhanced by a Claude API call (claude:vcs_enhancement). In production "
            "the KB would resolve and store the VCS URL once instead of calling an LLM."
        ),
        "project": project.get("name"),
        "version": version.get("versionName"),
        "totalCount": len(items),
        "items": items,
    }


def main() -> None:
    global MODEL
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="override project name from config")
    ap.add_argument("--version", help="override version name from config")
    ap.add_argument("--dry-run", action="store_true", help="fetch the BOM but skip the Claude call")
    ap.add_argument("--model", default=MODEL, help="Claude model (default: fast/economical demo model)")
    ap.add_argument("--out", type=Path, default=LIVE_DIR / "hub-api-components.json")
    args = ap.parse_args()
    MODEL = args.model

    cfg = load_config()
    project = args.project or cfg.get("project")
    version = args.version or cfg.get("version")
    if not project or not version:
        sys.exit("set project and version in blackduck.local.json (or pass --project/--version)")

    client = BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False))
    print(f"[provision] authenticated to {client.base}", flush=True)

    proj, ver, comps = resolve_project_version(client, project, version)
    print(f"[provision] {proj.get('name')}@{ver.get('versionName')}: "
          f"{len(comps)} components in BOM", flush=True)
    for c in comps:
        origins = ", ".join(o.get("externalId", "") for o in c.get("origins", []))
        print(f"    - {c.get('componentName')} {c.get('componentVersionName')}  [{origins}]", flush=True)

    if args.dry_run:
        print("[provision] --dry-run: skipping Claude enhancement", flush=True)
        return

    if "PASTE" in cfg.get("anthropic_api_key", ""):
        sys.exit("blackduck.local.json still has a placeholder anthropic_api_key — fill it in")

    anthropic_client = build_anthropic_client(cfg["anthropic_api_key"])
    print(f"[provision] resolving upstream repos via Claude ({MODEL}) ...", flush=True)
    resolved, usage = enhance_with_claude(anthropic_client, component_context(comps))

    print("[provision] Claude VCS resolution:", flush=True)
    for r in resolved:
        print(f"    - {r.component_name} {r.component_version}"
              f"  ->  {r.vcs_url or '(none)'}  [{r.confidence}"
              f"{'/' + r.default_branch if r.default_branch else ''}]", flush=True)
        print(f"        {r.rationale}", flush=True)
    print(f"[provision] token usage: in={usage.input_tokens} out={usage.output_tokens}", flush=True)

    manifest = to_watch_manifest(proj, ver, comps, resolved)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[provision] wrote watch manifest -> {args.out}", flush=True)
    print(f"[provision] load it in the monitor with:  python monitor/app.py --data-dir {args.out.parent}",
          flush=True)


if __name__ == "__main__":
    main()
