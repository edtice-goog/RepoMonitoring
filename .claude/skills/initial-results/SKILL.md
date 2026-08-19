---
name: initial-results
description: End-user onboarding — take a buildable C/C++ target from zero to results in BOTH Black Duck SCA and the RepoMonitoring monitor in one flow. Use when someone new wants "initial results", "onboard a project", "scan this and monitor it", or has never run this stack before. Combines the public Black Duck C/C++ skills (scan + compiler configuration) with this repo's ingestion.
---

# First results: one flow, two destinations

One capture run feeds both systems: `blackduck-c-cpp` uploads the BoM to
**Black Duck SCA**, and the same run's `cov_emit_links.json` ingests into the
**monitor**. Never run two captures.

## 0. Get the public Black Duck C/C++ skills

The scan itself is covered by the companion skill collection:

```bash
git clone https://github.com/edtice-goog/configure-blackduck-compilers
```

Its `skills/` directory provides `run-blackduck-c-cpp` (the scan) and
`configure-blackduck-compilers` (compiler-type fixes). Read and follow those
SKILL.md files for the scan mechanics; this skill covers what they don't: the
capture-quality gate and the monitor side.

## 1. Scan with blackduck-c-cpp → Black Duck SCA results

Follow `run-blackduck-c-cpp` from the collection, with these requirements this
stack adds (rationale in the local [run-analysis](../run-analysis/SKILL.md)
skill, which also has working build-script examples):

- The build command must be a **clean full rebuild of every shipped
  project/library** — Coverity only records what actually compiles.
- Keep the target's **`.git` checkout intact** at the exact built ref — it
  becomes ground-truth attribution and exact version pins in the monitor.
- Set `output_dir` somewhere durable: the `cov_emit_links.json` written there
  is the monitor's input.
- Credentials: BD url + api token per that skill (env var, never on disk).

When BD SCA results look thin or the log shows **unconfigured compiler /
"skipping" warnings** (common with embedded toolchains — IAR, Green Hills,
vendor GCCs): apply `configure-blackduck-compilers` from the collection, then
re-scan. An unconfigured compiler silently drops every TU it built.

## 2. Gate: is the capture real?

Before touching the monitor, sanity-check `<output_dir>/cov_emit_links.json`
(details in [run-analysis](../run-analysis/SKILL.md) step 4): the expected
source-language TUs are present, `link-units` is non-empty, and paths are real
checkout paths. A capture that fails this under-represents the build — fix the
build script or compiler configuration and re-scan; do not ingest it.

## 3. Ingest → monitor results

```bash
python provisioning/ingest.py --project <bd-project-name> --version <version> \
    --emit <output_dir>/cov_emit_links.json \
    --bd-url https://<the-bd-server-the-scan-used>/ \
    --monitor-url http://<monitor-host>:8378
```

Same machine as the build (checkout discovery is local). The monitor persists
the seeds, pulls the BoM from the BD project the scan just created, fuses it
with Claude-from-files reconstruction and `.git` provenance, and loads the
project live — no restart.

First time the monitor sees this BD **server**, the Recreate button (or the
ingest-triggered recreate) will ask for a **read** token once and store it in
the monitor's own credential store; the scan credential from step 1 is the
client's push credential and is never reused by the server.

## 4. Definition of done — both destinations show results

- **Black Duck SCA**: the project-version page has a BoM (the monitor's
  project header links straight to it).
- **Monitor** `GET /api/projects` lists the project; on its page, repos that
  own compiled source are **monitored** with pinned refs; header-only/linked
  components are **reference-only** with evidence tags.

Then, to make it live: "Check for updates" backfills commits since the built
release, and the [fill-triage](../fill-triage/SKILL.md) skill fills verdicts
cheaply. Optionally register upstream webhooks for push-driven monitoring.
