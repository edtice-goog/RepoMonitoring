"""RESEARCH SCRATCH — Black Duck KB / BoM-edit probe (task: bd-kb-link-research).

Not part of the monitor pipeline. Verifies, against a live Black Duck SCA
instance, how to (a) search the KB for a component by name, (b) add a KB
component-version to a project-version BoM, (c) observe securityRiskProfile
counts on the added entry, and (d) delete it again.

Writes NOTHING except to stdout / the --save file. All write operations are
behind explicit subcommands (add / delete) so a bare run is read-only.

Usage (PowerShell):
    $env:BLACKDUCK_LOCAL_CONFIG="blackduck.local.json"
    python -u scripts/kb_probe.py doc --grep components
    python -u scripts/kb_probe.py search --q "p256-m"
    python -u scripts/kb_probe.py bom --project repo-mon-mbedtls --version 3.6.3.1
    python -u scripts/kb_probe.py add --project ... --version ... --cv <componentVersionHref>
    python -u scripts/kb_probe.py delete --bom-entry <href>
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_scout import BDClient, load_config, meta_href  # noqa: E402

BOM_MEDIA = "application/vnd.blackducksoftware.bill-of-materials-6+json"
JSON_MEDIA = "application/json"


def raw(client, url, media=JSON_MEDIA, method="GET", body=None, extra=None):
    """Low-level request returning (status, headers, text) and never raising on
    an HTTP error status — we want to SEE 403/404/415 bodies, not stack traces."""
    if not url.startswith("http"):
        url = f"{client.base}{url}"
    headers = {"Authorization": f"Bearer {client.bearer}", "Accept": media}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = extra or media
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60, context=client.ctx) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def jget(client, url, media=JSON_MEDIA):
    st, _, txt = raw(client, url, media)
    if st != 200:
        return {"_status": st, "_body": txt[:500]}
    try:
        return json.loads(txt)
    except Exception:
        return {"_status": st, "_body": txt[:500]}


def find_version(client, project, version):
    projects = client.get_all("/api/projects")
    p = next((x for x in projects if x.get("name", "").lower() == project.lower()), None)
    if not p:
        sys.exit(f"no project {project!r}")
    vers = client.get_all(f"{meta_href(p)}/versions")
    v = next((x for x in vers if x.get("versionName", "").lower() == version.lower()), None)
    if not v:
        sys.exit(f"no version {version!r}; have "
                 + ", ".join(x.get("versionName", "?") for x in vers))
    return p, v


def cmd_doc(client, args):
    """Fetch the api-doc bundle (404s unauthenticated) and grep it."""
    for path in args.path:
        st, hdr, txt = raw(client, path, "text/html,application/json,*/*")
        print(f"\n=== {path} -> HTTP {st} ({hdr.get('Content-Type')}) len={len(txt)}")
        if st != 200:
            print(txt[:400])
            continue
        if args.save:
            Path(args.save).write_text(txt, encoding="utf-8")
            print(f"saved -> {args.save}")
        if args.grep:
            low = txt.lower()
            for term in args.grep:
                idxs = []
                i = low.find(term.lower())
                while i >= 0 and len(idxs) < args.hits:
                    idxs.append(i)
                    i = low.find(term.lower(), i + 1)
                print(f"-- '{term}': {len(idxs)} hit(s) shown")
                for i in idxs:
                    print("   ..." + txt[max(0, i - args.ctx):i + args.ctx].replace("\n", " ") + "...")


def cmd_search(client, args):
    """Try several KB search shapes for one query string."""
    q = urllib.parse.quote(args.q)
    shapes = [
        ("/api/search/components?q=" + q, JSON_MEDIA),
        ("/api/search/components?q=" + q,
         "application/vnd.blackducksoftware.component-detail-5+json"),
        ("/api/components?q=" + q, JSON_MEDIA),
        ("/api/components?q=" + q,
         "application/vnd.blackducksoftware.summary-4+json"),
    ]
    if args.extra:
        shapes = [(s, JSON_MEDIA) for s in args.extra]
    for path, media in shapes:
        st, hdr, txt = raw(client, path, media)
        print(f"\n=== {path}\n    Accept: {media} -> HTTP {st} len={len(txt)}")
        if st != 200:
            print("   " + txt[:300].replace("\n", " "))
            continue
        try:
            d = json.loads(txt)
        except Exception:
            print("   (non-json) " + txt[:200])
            continue
        print(f"   totalCount={d.get('totalCount')}")
        for it in (d.get("items") or [])[:args.limit]:
            print("   " + json.dumps(it)[:600])


def cmd_bom(client, args):
    p, v = find_version(client, args.project, args.version)
    print(f"project={p.get('name')} version={v.get('versionName')}")
    print(f"versionHref={meta_href(v)}")
    d = jget(client, f"{meta_href(v)}/components?limit=100", BOM_MEDIA)
    print(f"totalCount={d.get('totalCount')}")
    for c in d.get("items", []):
        if args.full:
            print(json.dumps(c, indent=2)[:6000])
        else:
            print(f"  - {c.get('componentName')} {c.get('componentVersionName')} "
                  f"match={c.get('matchTypes')} usages={c.get('usages')} "
                  f"risk={[(e.get('countType'), e.get('count')) for e in (c.get('securityRiskProfile') or {}).get('counts', []) if e.get('count')]}")
            print(f"    componentVersion={c.get('componentVersion')}")
            print(f"    entry={meta_href(c)}")


def cmd_add(client, args):
    p, v = find_version(client, args.project, args.version)
    url = f"{meta_href(v)}/components"
    body = json.loads(args.body) if args.body else {"component": args.cv}
    print(f"POST {url}\n  body={json.dumps(body)}\n  Content-Type={args.ctype}")
    st, hdr, txt = raw(client, url, BOM_MEDIA, "POST", body, extra=args.ctype)
    print(f"-> HTTP {st}")
    print("   Location: " + str(hdr.get("Location")))
    print("   " + txt[:1500])


def cmd_delete(client, args):
    print(f"DELETE {args.href}")
    st, hdr, txt = raw(client, args.href, BOM_MEDIA, "DELETE")
    print(f"-> HTTP {st}\n   {txt[:600]}")


def cmd_get(client, args):
    st, hdr, txt = raw(client, args.href, args.media)
    print(f"{args.href}\n Accept: {args.media} -> HTTP {st}")
    try:
        print(json.dumps(json.loads(txt), indent=2)[:args.max])
    except Exception:
        print(txt[:args.max])


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doc")
    d.add_argument("--path", nargs="*", default=["/api-doc/public.html"])
    d.add_argument("--grep", nargs="*")
    d.add_argument("--ctx", type=int, default=200)
    d.add_argument("--hits", type=int, default=5)
    d.add_argument("--save")
    d.set_defaults(fn=cmd_doc)

    s = sub.add_parser("search")
    s.add_argument("--q", required=True)
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--extra", nargs="*")
    s.set_defaults(fn=cmd_search)

    b = sub.add_parser("bom")
    b.add_argument("--project", required=True)
    b.add_argument("--version", required=True)
    b.add_argument("--full", action="store_true")
    b.set_defaults(fn=cmd_bom)

    a = sub.add_parser("add")
    a.add_argument("--project", required=True)
    a.add_argument("--version", required=True)
    a.add_argument("--cv", help="componentVersion href")
    a.add_argument("--body", help="raw JSON body override")
    a.add_argument("--ctype", default=BOM_MEDIA)
    a.set_defaults(fn=cmd_add)

    x = sub.add_parser("delete")
    x.add_argument("--href", required=True)
    x.set_defaults(fn=cmd_delete)

    g = sub.add_parser("get")
    g.add_argument("--href", required=True)
    g.add_argument("--media", default=JSON_MEDIA)
    g.add_argument("--max", type=int, default=4000)
    g.set_defaults(fn=cmd_get)

    args = ap.parse_args()
    cfg = load_config()
    client = BDClient(cfg["url"], cfg["api_token"], cfg.get("insecure_tls", False))
    print(f"[kb_probe] authenticated to {client.base}", flush=True)
    args.fn(client, args)


if __name__ == "__main__":
    main()
