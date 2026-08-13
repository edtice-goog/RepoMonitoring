# Sample artifacts (fictitious — for the deterministic demo)

All data in this directory is hand-crafted to *look like* tool output. Nothing
here was produced by a real BD/CPP run, a real SBOM export, or a real upstream
commit. The fictitious device is the **ACME GW-7100**, an embedded network
gateway built with a Yocto-style workflow.

## build-capture.json

Simulates the BD/CPP (Coverity Build Capture) output. The capture phase emits
three things — everything resolvable *from the build host at capture time*:

1. **The compiled-file inventory** — every source and header (the include
   closure matters: security fixes land in `.h` files too).
2. **Locally resolvable components** — files owned by the system package
   manager (dpkg/apt) are resolved to a component during capture
   (`resolution: "package_manager"`); the openssl entries model this. All
   other files defer to SCA-side matching (`resolution: "sca_match"`).
3. **`repos_detected`** — every VCS URL discoverable on disk during the same
   scan that enumerates the files: `.git` remotes and build-system metadata
   (Yocto `SRC_URI`). The capture phase does **no de-duplication** — a local
   fork and its upstream both get emitted (see the kernel entry: ACME's
   internal `linux-acme` fork as `origin`, kernel.org as `upstream`).
   Reconciling duplicates is the SCA side's job and out of scope here. Both
   URLs matter to monitoring: fixes land upstream first; the fork is what
   actually gets built. zlib shows the opposite case — tarball fetch, no
   `.git`, empty `vcs_urls`, resolution falls entirely to the SCA side.

**File-selection rationale:**

- **Kernel files** come from generic core subsystems any embedded gateway
  plausibly builds — net core, TCP/IP, PHY, serial, MMC, I2C, GPIO, ext4.
  Real paths from the kernel tree, but nothing tied to an identifiable
  vendor module, so kernel-savvy audiences aren't distracted.
- **Deliberately absent:** anything under `drivers/net/wireless/` (and
  Bluetooth). The GW-7100 has no radio. This absence is load-bearing — it's
  what makes the "upstream security fix in a driver we never built →
  suppressed" demo scenario work.
- **Userland components:** BusyBox 1.36.1 (the canonical embedded utility;
  its udhcp client is a network-reachable packet parser — the natural home
  for the quiet-security-fix alert scenario) and zlib 1.3.1 (small,
  universally recognized, parses untrusted input).
- **Paths carry a Yocto-flavored build prefix** (`/build/gw7100/tmp/work/...`)
  so the prototype's path-normalization step (stripping build roots before
  suffix-matching against upstream paths) demonstrates on realistic input.

## SBOM artifacts — three formats, same three components

Any one of these can drive provisioning; having all three lets the demo match
the audience's preference and shows the `SBOMSource` adapter seam working.

- **`sbom.cyclonedx.json`** — CycloneDX 1.5. The richest input for the repo
  resolver: `externalReferences` of type `vcs` hand it the upstream repo URL
  directly (resolution tier 1).
- **`sbom.spdx.json`** — SPDX 2.3 JSON. Repo URLs arrive via
  `downloadLocation` (`git+https://...`) and PURL `externalRefs` — still
  resolvable, slightly more parsing.
- **`hub-api-components.json`** — simulated Black Duck Hub REST response
  (shape modeled on the project-version components endpoint, simplified).
  Each component carries a `vcsUrl`: we **assume the KB serves an upstream
  VCS URL for known components**. If the real API doesn't expose this today,
  that's a prerequisite product enhancement, not a workflow concern — and
  KB-backed VCS resolution is part of the product pitch.

**Where the resolver's harder tiers (ecosystem lookup, override table) still
matter:** components Black Duck *doesn't* know. The common deployment is a
Yocto-built OS + custom software + proprietary third-party software whose
CycloneDX comes in from the vendor and gets merged by the SCA tool — those
vendor SBOM entries may have absent, private, or stale VCS references. zlib's
tarball `downloadLocation` in the SPDX file plants the related "code
disconnected from its repo" case.

## commit-events.json

Simulated upstream commit events. No manufactured diffs — just the facts the
monitoring side would observe: VCS URL, branch, commit hash, changed-file
paths (upstream-relative; matching them against the capture's prefixed paths
is the relevance filter's job).

**The commit hash is the unit of selection and triage.** A commit is selected
iff ≥1 changed file is in scope; all in-scope files of a commit are evaluated
together, one verdict per hash.

| Event | Commit touches | Outcome |
|---|---|---|
| `evt-001` | `dhcpc.c` + `packet.c` (both in scope, one commit) + a doc file | selected → one joint triage call → `response_required` |
| `evt-002` | wireless driver files only (never built) | not selected — suppressed before triage |
| `evt-003` | `net/core/skbuff.c` (in scope, ambiguous change) | selected → `needs_human_review` |
| `evt-004` | `zutil.c` (in scope) + `CMakeLists.txt` | selected → `not_meaningful` |

## triage-verdicts.json + the triage service

`triage-verdicts.json` is the truth table for the stub "LLM triage service"
(`../triage-service/server.py`), keyed by commit hash, with simulated LLM
rationale text. evt-002's commit is deliberately absent — it must never reach
triage. Unknown commits fail safe to `needs_human_review`.

Run the service: `python ../triage-service/server.py` (port 8377;
`GET /health`, `POST /triage {vcs_url, commit, files}`).
