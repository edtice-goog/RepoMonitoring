"""Access layer for the rendered-event cache (the rendered_event table).

Keeps the monitor's ProjectState out of the SQL weeds. The load path is the hot one
(startup); the reconcile path (mark_pending -> save_rows -> sweep) is the background
replay. Rows are keyed by (project_name, commit_sha); `save_rows` upserts and clears
pending, so a commit still present in the feed survives the sweep while a vanished one
(still pending) is removed.
"""

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from db.models import RenderedEvent, _utcnow
from db.session import SessionLocal

# Columns save_rows accepts from a caller-built row dict (payload holds the full result).
_COLS = ("project_name", "commit_sha", "component", "committed_at", "label", "payload")


def load(project_name):
    """Non-pending rows for a project, newest commit first -> list of payload dicts.

    If nothing is non-pending but pending rows exist (a replay was interrupted before its
    sweep), serve those stale rows anyway — a slightly-old render beats a blank dashboard;
    the next replay reconciles them."""
    order = (RenderedEvent.committed_at.desc().nulls_last(), RenderedEvent.id.desc())
    with SessionLocal() as s:
        base = select(RenderedEvent.payload).where(RenderedEvent.project_name == project_name)
        rows = s.execute(base.where(RenderedEvent.pending.is_(False)).order_by(*order)) \
            .scalars().all()
        if not rows:
            rows = s.execute(base.order_by(*order)).scalars().all()
    return list(rows)


def save_rows(rows):
    """Upsert rendered rows by (project, commit), clearing pending. Each row is a dict
    with the _COLS keys. Idempotent; safe to call for a handful of rows or thousands."""
    rows = [r for r in rows if r.get("commit_sha")]
    if not rows:
        return 0
    with SessionLocal() as s:
        for r in rows:
            vals = {k: r.get(k) for k in _COLS}
            stmt = insert(RenderedEvent).values(pending=False, updated_at=_utcnow(), **vals)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_rendered_project_commit",
                set_={"component": stmt.excluded.component,
                      "committed_at": stmt.excluded.committed_at,
                      "label": stmt.excluded.label,
                      "payload": stmt.excluded.payload,
                      "pending": False,
                      "updated_at": _utcnow()})
            s.execute(stmt)
        s.commit()
    return len(rows)


def mark_pending(project_name):
    """Flag every row of a project pending — the 'mark' of the mark-and-sweep replay.
    Rows stay readable (load() filters them out, but a swept-then-reloaded cache is only
    swapped in at the end), so this does not blank the dashboard."""
    with SessionLocal() as s:
        s.execute(update(RenderedEvent)
                  .where(RenderedEvent.project_name == project_name)
                  .values(pending=True))
        s.commit()


def sweep(project_name):
    """Delete rows still pending after a reconcile — commits no longer in the feed (or a
    previously mis-rendered event). Returns how many were removed."""
    with SessionLocal() as s:
        n = s.execute(delete(RenderedEvent)
                      .where(RenderedEvent.project_name == project_name,
                             RenderedEvent.pending.is_(True))).rowcount
        s.commit()
    return n


def remove(project_name, shas):
    """Delete specific commits' rows (audit fix: phantom events not in the repo)."""
    if not shas:
        return 0
    with SessionLocal() as s:
        n = s.execute(delete(RenderedEvent)
                      .where(RenderedEvent.project_name == project_name)
                      .where(RenderedEvent.commit_sha.in_(list(shas)))).rowcount
        s.commit()
    return n


def clear(project_name):
    """Drop the whole cache for a project (reset-feed / re-base)."""
    with SessionLocal() as s:
        n = s.execute(delete(RenderedEvent)
                      .where(RenderedEvent.project_name == project_name)).rowcount
        s.commit()
    return n
