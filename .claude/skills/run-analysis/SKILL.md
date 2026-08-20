---
name: run-analysis
description: Run a full BD/CPP (blackduck-c-cpp + Coverity) build-capture analysis of a C/C++ target and ingest it into the RepoMonitoring monitor. Use when asked to analyze, scan, capture, or ingest a build or component, or to re-run analysis after a build change.
---

# Run a build-capture analysis and ingest it

The goal is a **real capture**: Coverity records every translation unit the build
actually compiles, Black Duck identifies content, and the monitor fuses both with
`.git` provenance. The #1 cause of bad results is a partial build — see step 3.

## 1. Prerequisites (all machines differ — verify, don't assume)

- Coverity analysis tools: pass their root as `coverity_root` (e.g. `C:\Coverity\cov-analysis-win64-<ver>`).
- `blackduck-c-cpp` CLI: a venv install exists at `stage3/.venv/Scripts/blackduck-c-cpp.exe` in this workspace; any install works.
- `RepoMonitoring/blackduck.local.json` (gitignored) with `url` + `api_token` for the Black Duck instance the results should land on, plus `anthropic_api_key`. NEVER put the token in config.yaml or on a command line — the capture wrapper reads it into `BLACKDUCK_API_TOKEN`.
- The target checked out **with its `.git` intact** at the exact ref being built (`git switch --detach <tag>`). The `.git` gives ground-truth attribution and the exact built ref; without it, attribution falls back to file matching only.
- The monitor running (default `http://127.0.0.1:8378`; check `GET /health`).

## 2. Author the build script (`build_capture.bat` or .sh)

A standalone script that **clean-builds the target from source**:

- Delete/recreate the build dir first (`rmdir /s /q ... & cmake -S ... -B ...`).
  Coverity only records processes that RUN — an incremental build silently omits
  everything already up to date.
- Build **all** projects/libraries that ship in the artifact, not just the app.
  A library built in an earlier or separate step is invisible to the capture.
- Leave intentionally-prebuilt third parties (e.g. a vendored OpenSSL `.lib`)
  out of the script — they will correctly classify as reference-only.
- Libraries-only is fine and faster (e.g. mbedtls: `-DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF`).
- Test the script once OUTSIDE the capture before spending a capture run on it.

See `../mbedtls-capture/build_capture.bat` and `../stage3/build_capture.bat` for
working examples (MinGW gcc + Ninja, and MSVC via vcvars respectively).

If the target has several third-party dependencies and you want them all in the
BOM as **monitored** rather than reference-only, use the **`build-sbom-target`**
skill instead — it covers building a whole dependency stack from source in one
`build_cmd`, and catalogues the build-system traps (silent vcpkg fallback,
static-link defines, CMake 4.x policy removal, decorated MSVC lib names) that
otherwise cost many capture runs to discover. It ships complete working scripts
for git (7 components) and Subversion (9).

## 3. Configure and run the capture

`config.yaml` next to the build script:

```yaml
project_name: <bd-project-name>     # convention: repo-mon-<component>
project_version: <version-built>    # the actual tag, e.g. 3.6.3.1
bd_url: https://<bd-instance>/
build_dir: <dir containing the script>
build_cmd: <absolute path to build_capture.bat>
coverity_root: <cov-analysis root>
output_dir: <workspace>\bdcpp-output-<component>
modes: sig,bdba
verbose: True
```

Run via a wrapper that injects the token (copy `../mbedtls-capture/capture.bat`;
add a `vcvars64.bat` call only for MSVC builds). The BD upload phases can take
tens of minutes on slow instances — run it in the background and wait.

## 4. Sanity-check the emit BEFORE ingesting

Open `<output_dir>/cov_emit_links.json` and verify — a capture failing these
checks under-represents the build and must be re-run, not ingested:

- `translation-units` contains the **source-language TUs you expect** (`.c`
  primaries for a C library — a C++-only TU list for a target that uses C
  components means the build skipped them).
- `link-units` is **non-empty** (the link step ran and was recorded).
- The primary-file paths are real checkout paths, not copies in a staging dir.

## 5. Register with the monitor — writing files is NOT ingestion

**The single most common mistake**: running `scripts/attribute_capture.py` (or
`provisioning/recreate.py`) and stopping. Those scripts only write a data dir of
JSON files; the monitor knows nothing about them and the project will NOT
appear. The run is not done until step 6 passes. Two registration paths:

**(a) Normal path — the ingestion API** (durable: Postgres-seeded, server-side
recreate, survives restarts):

```bash
python provisioning/ingest.py --project <bd-project-name> --version <version> \
    --emit <output_dir>/cov_emit_links.json --monitor-url http://127.0.0.1:8378
```

Run this on the machine where the build's source paths exist (checkout discovery
walks the local filesystem). The response says `"recreate": "started"`; the
server persists to Postgres, recreates the artifacts, and loads the project
live — **no monitor restart**. Re-basing an existing project onto a new release:
add `--replace --reset-feed`. Caveat: the server-side recreate reads the
monitor's own `blackduck.local.json`, so this path only works for projects on
THAT Black Duck instance.

**(b) One-off / foreign-instance path** — when the project lives on a different
BD instance (run `attribute_capture.py` with
`BLACKDUCK_LOCAL_CONFIG=<other-config>.json`), or a recreated data dir already
exists, you MUST register it yourself:

```bash
curl -X POST "http://127.0.0.1:8378/projects/add?project=<name>&data_dir=<abs-path-to-data-dir>"
```

This loads it live without a restart and records it so restarts reload it.

## 6. Verify — the definition of done

- `GET /health` project count went up; `GET /api/projects` **lists the project**.
  If it does not, registration did not happen — go back to step 5; do not
  conclude the monitor needs a restart (it never does for new projects).
- On the project page: the expected repos are **monitored** (own compiled
  sources); linked/header-only components are **reference-only**; the pinned
  ref equals the tag actually built (bold ref + struck-out label means the SCA
  KB version conflicted and local ground truth won — expected, not an error).
- Then: "Check for updates" backfills `<pinned tag>..<watch branch>`; use the
  fill-triage skill (cheap) or the Fill button (Claude API) for verdicts.
