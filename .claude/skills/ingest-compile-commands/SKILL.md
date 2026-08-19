---
name: ingest-compile-commands
description: Build a monitor project from a compile_commands.json alone — the emailed-SBoM-verification scenario. Use when someone has a compilation database but no buildable tree, no Coverity emit, no .git checkouts, and typically no Black Duck project ("I got this compile_commands.json / SBoM by email, what's really in this build?"). The Claude-from-files reconstruction still identifies the components.
---

# Ingest a compilation database (no build, no git, no BD)

The typical trigger: an SBoM arrives that you don't trust, accompanied by (or
answerable with) a `compile_commands.json`. That file names every translation
unit the build compiles and every include directory it searches — enough
evidence for the server's Claude-from-files reconstruction to identify the
real third-party components, resolve their upstream repos and watch branches,
and stand up a monitor project to compare against the claimed SBoM.

## Run it

```bash
python provisioning/ingest_compile_commands.py \
    --project <name> --version <label> \
    --ccdb <path>/compile_commands.json \
    --monitor-url http://127.0.0.1:8378
```

That is the whole flow: the converter extracts the TUs (structured `file`
fields) and include directories (`-I`/`-isystem`/`/I` flags, resolved against
each entry's `directory`), POSTs the standard ingestion payload, and the
server recreates + loads the project live. `--dump` prints the payload
instead of POSTing, for inspection or handoff.

- `--bd-url` defaults to **empty** = no BD SCA association. That is valid and
  expected here; recreate skips the BoM pull and needs no BD credential. Pass
  a server URL only if a real BD project exists for this build.
- `--version` is just the project label — per-component versions will be
  Claude estimates regardless (see honesty notes below).

## What to check when it loads

- `GET /api/projects` lists the project (nothing appears until ingestion —
  see [run-analysis](../run-analysis/SKILL.md) step 5 if it is missing).
- On the project page: components whose sources compile are **monitored**;
  components known only from include paths are **reference-only** with
  `evidence: headers-only`.
- Compare the component list against the SBoM being questioned. The union is
  built to over-approximate: a component in the manifest but not the SBoM is
  the interesting finding.

## Honest limits of this mode

- **Versions are Claude best-estimates** — no KB identity, no `.git` ref to
  pin against. The UI marks them "≈ inferred". Treat them as hypotheses; the
  watch branch is still usually right, and the pinned tree ref falls back to
  the default branch when the estimate resolves to no tag.
- **No provenance**: no built-from repo, no divergence detection, no exact
  backfill base. "Check for updates" walks from the (estimated) release tag.
- **Invisible components**: anything neither compiled nor named by an include
  path (a prebuilt `.a` with no headers in the include search path) cannot be
  seen. Only a real capture plus BDBA binary matching covers those — offer
  [initial-results](../initial-results/SKILL.md) when the build becomes
  available.
- Path shape matters: if the database came from a build that flattened or
  anonymized paths, suffix attribution weakens (check that TU paths still
  resemble upstream layouts).
