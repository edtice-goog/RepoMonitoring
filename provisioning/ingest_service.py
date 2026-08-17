"""Server-side ingestion: turn a posted seed payload into the three Postgres seed tables.

This is the persistence half of ingestion, and it makes NO assumption about the local
filesystem — every byte it needs (the compiled file set + the per-checkout provenance
facts) arrives in the payload. In production the build runs on a remote Linux/macOS box
that POSTs the payload to the monitor; the monitor calls persist_ingest() here. The only
external touch is the GitHub fork-parent lookup (checkout origin -> canonical upstream),
kept on this side so the build box needs no GitHub token.

Payload schema (see provisioning/ingest.py for the producing client):
    {
      "schema": 1,
      "project": "repo-mon-stage3-curl",
      "version": "8.21.0",
      "bd_url":  "<Black Duck SCA project link>",   # optional; the SBoM is NOT sent
      "replace": true,
      "files":   [{"path": "...", "is_primary": true, "kind": "source"}, ...],
      "checkouts":[{"actual_source_url": "https://github.com/edtice-goog/zlib",
                    "actual_source_ref": "59933eca9041 (v1.3.1-1-g59933ec)"}, ...]
    }
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

SCHEMA_VERSION = 1


class IngestError(ValueError):
    """Payload is malformed or conflicts with existing state (maps to HTTP 4xx)."""


def _validate(payload):
    if not isinstance(payload, dict):
        raise IngestError("payload must be a JSON object")
    schema = payload.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise IngestError(f"unsupported payload schema {schema!r} (expected {SCHEMA_VERSION})")
    name = (payload.get("project") or "").strip()
    version = (payload.get("version") or "").strip()
    if not name or not version:
        raise IngestError("payload missing project and/or version")
    files = payload.get("files") or []
    if not isinstance(files, list) or not files:
        raise IngestError("payload has no files (nothing to ingest)")
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            raise IngestError("every file entry needs a 'path'")
    checkouts = payload.get("checkouts") or []
    if not isinstance(checkouts, list):
        raise IngestError("'checkouts' must be a list")
    return name, version, files, checkouts


def enrich_checkouts(checkouts, gh, log=print):
    """Resolve each posted checkout's origin to its canonical upstream (fork-parent) and
    build provenance rows, merged by ground-truth (a repo watched once even if two
    checkouts share it). This is the one network step of ingestion."""
    from attribute_capture import fork_parent, norm_repo
    prov = {}
    for co in checkouts:
        asrc = (co or {}).get("actual_source_url")
        if not asrc:
            continue
        can = fork_parent(gh, asrc)
        nc = norm_repo(can)
        if nc not in prov:
            div = norm_repo(asrc) != nc
            prov[nc] = {"ground_truth_url": can, "actual_source_url": asrc,
                        "actual_source_ref": co.get("actual_source_ref"), "divergent": div}
            log(f"    provenance: {asrc}@{co.get('actual_source_ref')} -> {can}"
                + ("  DIVERGENT" if div else ""))
    return prov


def persist_ingest(payload, gh, log=print):
    """Validate + persist a seed payload to Postgres (project / capture / capture_file /
    source_provenance). Returns a summary the API echoes back to the client. Idempotent
    with replace=true (an existing project is deleted and re-created)."""
    from db.models import Project, Capture, CaptureFile, SourceProvenance
    from db.session import SessionLocal
    from emit_local import HDR_EXT

    name, version, files, checkouts = _validate(payload)
    bd_url = (payload.get("bd_url") or "").strip()
    replace = bool(payload.get("replace"))

    prov = enrich_checkouts(checkouts, gh, log=log)

    with SessionLocal() as s:
        existing = s.query(Project).filter_by(name=name).one_or_none()
        was_replaced = existing is not None
        if existing is not None:
            if not replace:
                raise IngestError(f"project {name!r} already ingested (pass replace=true)")
            s.delete(existing)
            s.flush()

        proj = Project(name=name, bd_project_url=bd_url, bd_version=version)
        s.add(proj)
        s.flush()

        cap = Capture(project_id=proj.id, build_id=f"{name}@{version}")
        s.add(cap)
        s.flush()
        s.add_all([
            CaptureFile(capture_id=cap.id, path=f["path"],
                        is_primary=bool(f.get("is_primary")),
                        kind=f.get("kind")
                             or ("header" if f["path"].lower().endswith(HDR_EXT) else "source"))
            for f in files
        ])
        for pr in prov.values():
            s.add(SourceProvenance(project_id=proj.id, **pr))

        s.commit()
        pid = proj.id

    log(f"[ingest] project={name} id={pid}: {len(files)} capture files, "
        f"{len(prov)} provenance rows" + (" (replaced)" if was_replaced else ""))
    return {
        "project": name, "id": pid, "version": version, "bd_url": bd_url,
        "replaced": was_replaced, "files": len(files),
        "provenance": [{"actual_source": pr["actual_source_url"],
                        "ground_truth": pr["ground_truth_url"],
                        "ref": pr["actual_source_ref"], "divergent": pr["divergent"]}
                       for pr in prov.values()],
    }
