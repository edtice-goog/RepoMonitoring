"""Package the RepoMonitoring demo into a self-contained, run-anywhere zip.

Produces dist/repomonitoring-demo.zip containing everything needed to run the
demo on any machine with Python 3.8+ (stdlib only — no pip installs). Contents
unzip into a single repomonitoring-demo/ folder; see DEMO.md inside for the
run instructions.

Usage:  python package.py
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
TOP = "repomonitoring-demo"  # single top-level folder inside the zip

INCLUDE = [
    "DEMO.md",
    "DESIGN.md",
    "monitor/app.py",
    "triage-service/server.py",
    "driver/replay.py",
    "samples/README.md",
    "samples/build-capture.json",
    "samples/sbom.cyclonedx.json",
    "samples/sbom.spdx.json",
    "samples/hub-api-components.json",
    "samples/commit-events.json",
    "samples/triage-verdicts.json",
]


def main() -> None:
    missing = [rel for rel in INCLUDE if not (ROOT / rel).is_file()]
    if missing:
        raise SystemExit(f"refusing to package — missing files: {missing}")

    DIST.mkdir(exist_ok=True)
    out = DIST / "repomonitoring-demo.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE:
            zf.write(ROOT / rel, f"{TOP}/{rel}")

    size_kb = out.stat().st_size / 1024
    print(f"Wrote {out} ({size_kb:.0f} KB, {len(INCLUDE)} files)")
    print(f"Unzip anywhere and follow {TOP}/DEMO.md — requires only Python 3.8+.")


if __name__ == "__main__":
    main()
