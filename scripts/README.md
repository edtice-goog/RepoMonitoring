# Live-mode helper scripts

These scripts produce the **Stage 2 "live provisioned"** demo data (see
`../DEMO.md` → *Live mode*). The core offline demo does **not** use them and
needs no dependencies. Everything here reads `../blackduck.local.json`
(gitignored) and writes to `../live/` (gitignored).

Install the one extra dependency first: `pip install -r ../requirements-live.txt`.

| Script | What it does | Reads | Writes | Network |
|---|---|---|---|---|
| `bd_scout.py` | Explore the Black Duck instance to find a good demo target (a project version with a 3–7 component BOM). | `blackduck.local.json` | — (prints) | Black Duck |
| `bd_provision.py` | Pull the live BOM for the configured `project`/`version`; resolve each component's upstream VCS URL with a Claude API call (`client.messages.parse`, `claude-opus-5`). | `blackduck.local.json` | `live/hub-api-components.json` | Black Duck + Claude |
| `gh_replay.py` | For each resolved GitHub repo, fetch the most recent commits on its maintenance branch and a source-file index at the release tag; save both in the shapes the monitor/driver consume. | `live/hub-api-components.json` | `live/winscp-commit-events.json`, `live/build-capture.json` | GitHub (`gh auth token`) |

## Notes

- **Credentials** live only in `blackduck.local.json` (Black Duck access token +
  Anthropic API key). It is gitignored; never commit it. `bd_scout.py` reads the
  token straight from the file so it never lands on a command line.
- **VCS URLs are not served by the Black Duck BOM endpoint today**, so
  `bd_provision.py` enhances them with Claude. In a real product the KB would
  resolve and store the VCS URL once, not call an LLM per run — the LLM call here
  demonstrates the potential end to end.
- **The compiled-file index from `gh_replay.py` is an approximation**
  (`kind: gh_tree_approx`) — the component's released source tree standing in for
  a real BD/CPP Coverity capture (Stage 3), which requires being able to build
  the target. OpenSSL 1.1.1s is watched on its `OpenSSL_1_1_1-stable` maintenance
  branch, not `master` — fixes land on the stable branch for an EOL release line.
- **Idempotent + polite:** commit history for an old release tag is static, so
  fetch once and replay `live/*.json` offline instead of re-hitting GitHub.
