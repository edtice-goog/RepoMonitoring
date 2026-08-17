"""Stub "LLM triage service" for the RepoMonitoring demo.

Plays the role ClaudeTriage will fill in production: accepts a commit hash +
VCS URL (+ the in-scope files the relevance filter matched) and returns a
triage verdict. Deterministic by design — verdicts come from a truth table
keyed by commit hash, so the demo is repeatable with no API keys, no network,
no token spend.

The commit hash is the unit of triage: one request per selected commit, all
in-scope files evaluated together, one verdict back.

Usage:
    python server.py [--port 8377] [--verdicts ../samples/triage-verdicts.json]

API:
    GET  /health   -> {"status": "ok"}
    POST /triage   {"vcs_url": "...", "commit": "<sha>", "files": ["..."]}
                   -> TriageResult JSON (failsafe: needs_human_review for
                      commits not in the truth table)
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_VERDICTS = Path(__file__).resolve().parent.parent / "samples" / "triage-verdicts.json"


class TriageHandler(BaseHTTPRequestHandler):
    verdicts: dict = {}

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "triage-stub"})
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/triage":
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "body must be valid JSON"})
            return

        commit = request.get("commit")
        if not commit:
            self._send_json(400, {"error": "missing required field: commit"})
            return

        result = self.verdicts.get(commit) or self.verdicts["_failsafe_default"]
        response = {
            "commit": commit,
            "vcs_url": request.get("vcs_url"),
            "files_evaluated": request.get("files", []),
            **result,
        }
        # Cross-repo mirrors: the monitor found the same physical file compiled
        # into other repos. A security fix here likely needs propagating — the
        # mirrored copy is probably behind. (Deterministic note; verdict unchanged.)
        cross = request.get("cross_repo") or []
        if cross:
            names = ", ".join(f"{c.get('component')} ({c.get('path')})"
                              + (" [divergent]" if c.get("divergent") else "") for c in cross)
            response["cross_repo_locations"] = cross
            response["cross_repo_advice"] = (
                f"Same file is also compiled into: {names}. If security-relevant, "
                f"propagate the fix; the mirrored copy is likely behind upstream.")
            if response.get("recommended_action"):
                response["recommended_action"] += f" Also patch mirrored copies: {names}."
        self._send_json(200, response)

    def log_message(self, fmt, *args):  # one-line request log, no noise
        print(f"[triage-stub] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--verdicts", type=Path, default=DEFAULT_VERDICTS)
    args = parser.parse_args()

    table = json.loads(args.verdicts.read_text(encoding="utf-8-sig"))
    table.pop("_comment", None)
    TriageHandler.verdicts = table

    known = [k for k in table if not k.startswith("_")]
    print(f"[triage-stub] serving on http://127.0.0.1:{args.port}  "
          f"({len(known)} commits in truth table, failsafe=needs_human_review)")
    HTTPServer(("127.0.0.1", args.port), TriageHandler).serve_forever()


if __name__ == "__main__":
    main()
