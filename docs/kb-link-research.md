# KB-link research: adding Claude-identified components to the BD SCA BoM

*Research task `bd-kb-link-research`, 2026-08-20. The scheduled run was killed
mid-flight by a dropped permission stream (transcript `4ec6aaa9…`, 17:36 EDT);
findings were completed interactively the same evening. Probe tool:
`scripts/kb_probe.py` (research scratch, not part of the pipeline).*

## Goal

Components identified only by the Claude-from-files reconstruction
(`versionSource: "claude-inferred"`) are absent from the BD project BoM, so the
monitor's "Known vulns (BD SCA)" column shows an em-dash for them. Chosen
direction (user-confirmed, API support confirmed by a BD expert): **add them to
the BoM via REST**, after which the existing `securityRiskProfile → vulnCounts`
pipeline picks them up with no monitor changes.

## Verified endpoints (field-test instance, 2026-08-20)

All verified live. The API doc bundle is at `/api-doc/public.html` — it returns
**404 (not 401) when unauthenticated**; fetch with a bearer token.

### 1. KB search by external id — the primary join

    GET /api/components?q=github:<owner>/<repo>[:<tag>]
    Accept: application/vnd.blackducksoftware.component-detail-4+json

`q` takes **external ids only** (`github:…`, `maven:…`, `id:npmjs|…`) — free-text
names always return `totalCount: 0`, which is a trap (HTTP 200, empty). Hits
return `componentName`, `versionName`, and **hrefs** for component / version /
variant.

### 2. KB search by package URL — equivalent, slightly narrower

    GET /api/search/kb-purl-component?purl=pkg:github/<owner>/<repo>[@<tag>]
    Accept: application/vnd.blackducksoftware.component-detail-5+json

Same shape of answer. In practice the external-id search resolved two
components the purl search missed (CMSIS_5, mcux-sdk) — use external-id first,
purl as fallback.

### 3. Name search — human-facing fallback, NO hrefs

    GET /api/search/kb-components?q=<name>
    Accept: application/vnd.blackducksoftware.report-1+csv

Returns CSV rows (name, license, riskProfile columns) with **no hrefs**, so it
cannot drive an automated add. Useful only to tell "not in KB" from "in KB
under another identity" when the id searches miss.

### 4. Add to BoM (verified by reversible trial)

    POST /api/projects/{projectId}/versions/{projectVersionId}/components
    Content-Type: application/vnd.blackducksoftware.bill-of-materials-6+json
    {"component": "<component-version href from search>"}

### 5. Remove from BoM (the rollback)

    DELETE <BoM entry _meta.href>        → HTTP 204

## Trial transcript (add → counts → rollback)

Project `repo-mon-mbedtls@3.6.3.1`, BoM before: `['mbed TLS']`. Added zlib
1.3.1's component-version href (from the purl search):

- `POST …/components` → **HTTP 200** (no Location header; entry visible on the
  next BoM GET).
- **3 seconds later** the entry was present with
  `matchTypes: ['MANUAL_BOM_COMPONENT']` and `securityRiskProfile` **already
  populated** — `LOW: 2`, matching zlib 1.3.1's counts in the stage3 project.
  No async KB job wait was observed.
- `DELETE <entry href>` → **HTTP 204**; BoM back to `['mbed TLS']` exactly.

The monitor's existing read token performed both writes — **no separate
credential tier needed** on this instance (role includes BoM edit).

## Search results for the real examples

| Component (watch) | github external-id search | Verdict |
|---|---|---|
| mbed TLS (proposed v3.6.3) | `github:Mbed-TLS/mbedtls:v3.6.3` → **mbed TLS v3.6.3** | resolvable, incl. exact version |
| zlib v1.3.1 (control) | `github:madler/zlib:v1.3.1` → **zlib 1.3.1** | resolvable |
| CMSIS | `github:ARM-software/CMSIS_5` → **CMSIS_5** (no version) | resolvable, version TBD from KB version list |
| NXP MCUXpresso SDK | `github:nxp-mcuxpresso/mcux-sdk` → **nxp-mcuxpresso/mcux-sdk** | resolvable |
| p256-m | `github:mpg/p256-m` → 0; name search: only unrelated (`webauthn-p256`, `p256-cortex-m4`) | **not in KB** |
| Project Everest / HACL* | `github:project-everest/hacl-star` → 0 | **not in KB** |
| SEGGER RTT | `github:SEGGERMicro/RTT` → 0; name search: js wrappers only | **not in KB** |
| QP/C++ (QP-nano) | `github:QuantumLeaps/qpcpp` → 0; name search has a bare `qp` (identity unconfirmed) | probably not in KB; needs human look |

Honest takeaway: the KB gap **is** why BD missed these components in the first
place. Add-to-BoM fixes the resolvable half (CMSIS, mcux-sdk, and version
corrections like mbed TLS v3.6.3); p256-m / HACL* / SEGGER RTT cannot get BD
counts because the KB has no such components — their em-dash is correct and
should stay (tooltip already explains it). OSV/upstream monitoring remains
their only signal, which the monitor already provides.

## Proposed integration (not yet implemented)

Follow the project's explicit-button pattern (Fill / Refresh SCA data / OSV
cross-check) — **no automatic BoM writes during recreate**:

1. Per-watch action **"Add to BD BoM"** shown only for watches with
   `vuln_counts is None` and a project `bd_project_url`. Confirmation dialog
   states exactly what will be POSTed.
2. Server side: external-id search from the watch's `vcsUrl` owner/repo
   (+ pinned tag when present), purl fallback, cached via
   `services/cache.py cached("kb-search", …)`. Zero hits → the button reports
   "not in the Black Duck KB" and the em-dash stays.
3. Version pick: exact tag hit if the search resolves it; else list KB versions
   of the component and pick the nearest by version-digit compare; label the
   provenance honestly (`vulnCountsSource: "bom:monitor-added(version≈)"`).
4. POST the add with the monitor's stored read credential (verified
   sufficient), then trigger the normal **Refresh SCA data** flow — the counts
   arrive through the existing sbom pull with `matchTypes:
   MANUAL_BOM_COMPONENT` visible in BD's own UI for auditability.
5. API cost per add: 1–2 search GETs (cached) + 1 POST + the recreate's
   existing BoM pull. No polling needed (counts were immediate).

## Open questions

- `qp` in the KB name search: is it QP/C++? Needs a human look at the KB entry
  (no href from CSV; try `github:QuantumLeaps/qpc` and the KB UI).
- sca233 (SF90) was NOT touched (write policy). Its BoM is empty; the add flow
  is the same POST, but verify the sca233 token's role allows BoM edits before
  wiring the button for that instance.
- BDSA-vs-CVE split: `securityRiskProfile` merges sources into severity counts.
  If the UI ever needs the BDSA/CVE breakdown, the per-entry
  `/vulnerable-bom-components` view is the place to look (not probed — counts
  were the requirement).
