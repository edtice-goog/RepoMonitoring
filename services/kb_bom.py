"""Add a monitor watch's component to its Black Duck SCA project BoM.

The path for components the Claude-from-files reconstruction identified but BD
signature-matching missed: search the BD KnowledgeBase by the watch's repo
identity, add the best-match component-version to the project-version BoM
(entry appears as MANUAL_BOM_COMPONENT, securityRiskProfile populates
immediately — verified live, docs/kb-link-research.md), and hand the counts
back so the UI updates without waiting for a full SCA refresh.

Search strategy (from the research): external-id search
`/api/components?q=github:owner/repo[:tag]` is the primary join (returns
hrefs), the purl search is the fallback; free-text names return HTTP 200 with
zero items and are useless here. A component that misses BOTH searches is not
in the KB at all — that is a truthful terminal state (the user's remedy is a
BD support ticket to get the KB extended), not an error to retry.

All requests use the monitor's stored read credential (verified sufficient for
BoM edits on the field-test instance). Nothing here is called automatically —
the monitor exposes it behind an explicit per-watch button with confirmation.
"""

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

DETAIL4 = "application/vnd.blackducksoftware.component-detail-4+json"
DETAIL5 = "application/vnd.blackducksoftware.component-detail-5+json"
BOM6 = "application/vnd.blackducksoftware.bill-of-materials-6+json"
JSON_MEDIA = "application/json"


class KBError(Exception):
    """Failure the UI should show verbatim (auth, HTTP, no-KB-match…)."""


class _Client:
    """Minimal bearer-auth BD client (mirrors scripts/bd_scout.BDClient, kept
    local so the monitor's runtime does not import from scripts/)."""

    def __init__(self, base_url, api_token, insecure=False):
        self.base = base_url.rstrip("/")
        self.ctx = ssl._create_unverified_context() if insecure else None
        req = urllib.request.Request(
            f"{self.base}/api/tokens/authenticate", method="POST",
            headers={"Authorization": f"token {api_token}"}, data=b"")
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                self.bearer = json.loads(r.read())["bearerToken"]
        except Exception as exc:
            raise KBError(f"BD authentication failed: {exc}")

    def call(self, url_or_path, media=JSON_MEDIA, method="GET", body=None):
        url = url_or_path if url_or_path.startswith("http") \
            else f"{self.base}{url_or_path}"
        headers = {"Authorization": f"Bearer {self.bearer}", "Accept": media}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = media
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                txt = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(txt) if txt.strip() else {})
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:300]
            return e.code, {"_error": txt}
        except Exception as exc:
            raise KBError(f"BD request failed ({method} {url[:80]}…): {exc}")


def _owner_repo(vcs_url):
    m = re.search(r"github\.com[:/]+([^/]+)/([^/#?]+)", vcs_url or "", re.I)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"


def _ver_digits(v):
    return [int(x) for x in re.findall(r"\d+", v or "")][:4]


def _nearest_version(versions, proposed):
    """KB version whose digit-tuple is closest to the proposed version (exact
    digit match preferred). Returns (version_item, exact: bool)."""
    want = _ver_digits(proposed)
    best, best_key = None, None
    for it in versions:
        have = _ver_digits(it.get("versionName"))
        if want and have == want:
            return it, True
        # distance: compare digit-by-digit, missing digits count high
        key = sum(abs((have[i] if i < len(have) else 0)
                      - (want[i] if i < len(want) else 0)) * (1000 ** (3 - i))
                  for i in range(4)) if want else len(have)
        if best_key is None or key < best_key:
            best, best_key = it, key
    return best, False


def _search(client, owner_repo, tag):
    """(hit, how) — hit has component/version hrefs; how names the query that
    matched. Tries external-id with tag, external-id bare, purl with tag, purl
    bare. None when the KB has nothing for this repo."""
    tries = []
    if tag:
        tries.append((f"/api/components?q=" + urllib.parse.quote(f"github:{owner_repo}:{tag}"),
                      DETAIL4, f"github:{owner_repo}:{tag}"))
    tries.append((f"/api/components?q=" + urllib.parse.quote(f"github:{owner_repo}"),
                  DETAIL4, f"github:{owner_repo}"))
    purl = f"pkg:github/{owner_repo}"
    if tag:
        tries.append(("/api/search/kb-purl-component?purl="
                      + urllib.parse.quote(f"{purl}@{tag}", safe=""),
                      DETAIL5, f"purl {purl}@{tag}"))
    tries.append(("/api/search/kb-purl-component?purl="
                  + urllib.parse.quote(purl, safe=""), DETAIL5, f"purl {purl}"))
    for path, media, how in tries:
        st, d = client.call(path, media)
        if st == 200 and (d.get("items") or []):
            return d["items"][0], how
    return None, None


def _bom_counts(client, version_href, component_name):
    """securityRiskProfile counts for one component in the project BoM, shaped
    like the manifest's vulnCounts ({severity: n}, OK omitted)."""
    st, d = client.call(f"{version_href}/components?limit=100", BOM6)
    if st != 200:
        return None
    for it in d.get("items", []):
        if (it.get("componentName") or "").lower() == component_name.lower():
            return {e["countType"]: e["count"]
                    for e in (it.get("securityRiskProfile") or {}).get("counts", [])
                    if e.get("countType") != "OK" and e.get("count")}
    return None


def _project_version_href(client, bd_project_name, bd_version):
    st, d = client.call("/api/projects?limit=200&q=name:"
                        + urllib.parse.quote(bd_project_name))
    items = d.get("items") or [] if st == 200 else []
    p = next((x for x in items
              if x.get("name", "").lower() == bd_project_name.lower()), None)
    if p is None:
        raise KBError(f"BD project {bd_project_name!r} not found")
    href = p["_meta"]["href"]
    st, d = client.call(f"{href}/versions?limit=100")
    v = next((x for x in (d.get("items") or [])
              if x.get("versionName", "").lower() == bd_version.lower()), None)
    if v is None:
        raise KBError(f"version {bd_version!r} not found on BD project "
                      f"{bd_project_name!r}")
    return v["_meta"]["href"]


def add_watch_to_bom(server, api_token, insecure, bd_project, bd_version,
                     component_name, vcs_url, pinned_ref):
    """The whole flow: search KB -> pick version -> POST to BoM -> read counts.

    Returns {added, message, vuln_counts, kb_component, kb_version, approximate,
    matched_by}. `added` False + message explains why (incl. the truthful
    not-in-KB terminal state)."""
    owner_repo = _owner_repo(vcs_url)
    if not owner_repo:
        return {"added": False, "message":
                f"{component_name}: no GitHub URL on this watch — KB search "
                f"needs a repo identity (owner/repo)."}
    client = _Client(server, api_token, insecure)
    hit, how = _search(client, owner_repo, pinned_ref)
    if hit is None:
        return {"added": False, "message":
                f"{component_name}: not in the Black Duck KB (searched "
                f"github:{owner_repo} and the purl equivalent). If this "
                f"component matters, open a Black Duck support ticket asking "
                f"for it to be added to the KnowledgeBase — this button will "
                f"work once it lands."}

    approximate = False
    cv_href = hit.get("version")
    kb_version = hit.get("versionName")
    if not cv_href:
        # Component-level hit only: choose the nearest KB version to the
        # watch's pinned ref, and say so honestly.
        st, d = client.call(hit["component"].rstrip("/") + "/versions?limit=200",
                            DETAIL4)
        versions = (d.get("items") or []) if st == 200 else []
        if not versions:
            return {"added": False, "message":
                    f"{component_name}: KB component "
                    f"{hit.get('componentName')!r} has no listed versions."}
        best, exact = _nearest_version(versions, pinned_ref)
        cv_href = best["_meta"]["href"] if best.get("_meta") else best.get("version")
        kb_version = best.get("versionName")
        approximate = not exact

    pv_href = _project_version_href(client, bd_project, bd_version)
    st, d = client.call(f"{pv_href}/components", BOM6, "POST",
                        {"component": cv_href})
    if st not in (200, 201):
        raise KBError(f"BoM add rejected: HTTP {st} {d.get('_error', '')[:200]}")

    counts = _bom_counts(client, pv_href, hit.get("componentName") or component_name)
    return {"added": True, "vuln_counts": counts,
            "kb_component": hit.get("componentName"), "kb_version": kb_version,
            "approximate": approximate, "matched_by": how,
            "message": (f"{component_name}: added KB component "
                        f"{hit.get('componentName')!r} {kb_version or ''}"
                        + (" (nearest version — approximate)" if approximate else "")
                        + f" to the BD BoM (matched by {how}). "
                        + (f"Known vulns now: {sum(counts.values())}." if counts
                           else "Counts pending — press Refresh SCA data."))}
