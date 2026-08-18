"""Postgres schema — the small, authoritative set of things we CANNOT recreate.

Everything recreatable (the BD SBoM, Claude outputs, GitHub trees/commits/diffs,
triage verdicts) lives in the Redis cache, never here. These three tables hold only
the seeds a full recreation cannot rebuild from an external call:

  project            - the BD SCA project LINK + identity (not the SBoM).
  capture/capture_file - the distilled Coverity compiled-file set (idir is ephemeral).
  source_provenance  - the .git-discovery result: the authoritative VCS link + exact
                       release ref (the local checkout is ephemeral, the reference is not).

Ingested once per release build (provisioning/ingest.py); read by every recreate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    bd_project_url: Mapped[str] = mapped_column(Text)          # the BD SCA project link
    bd_version: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    captures: Mapped[list["Capture"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    provenance: Mapped[list["SourceProvenance"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")


class Capture(Base):
    __tablename__ = "capture"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True)
    build_id: Mapped[str] = mapped_column(String(200))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="captures")
    files: Mapped[list["CaptureFile"]] = relationship(
        back_populates="capture", cascade="all, delete-orphan")


class CaptureFile(Base):
    __tablename__ = "capture_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    capture_id: Mapped[int] = mapped_column(
        ForeignKey("capture.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)                    # compiled file path
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)  # primary TU?
    kind: Mapped[str] = mapped_column(String(16))             # source|header

    capture: Mapped["Capture"] = relationship(back_populates="files")


class SourceProvenance(Base):
    __tablename__ = "source_provenance"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True)
    ground_truth_url: Mapped[str] = mapped_column(Text)       # canonical upstream (watched)
    actual_source_url: Mapped[str] = mapped_column(Text)      # checkout we built (may be fork)
    actual_source_ref: Mapped[str] = mapped_column(String(200))  # exact rev (sha + describe)
    divergent: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship(back_populates="provenance")


class EventCursor(Base):
    """Per (project, repo) high-water mark for the 'check for updates' backfill.

    Operational state (not a seed): the newest upstream commit we've already
    surfaced as an event. On the first check for a repo it is absent, so the
    backfill starts at the built release tag's date and replays forward to now;
    thereafter it only pulls what is newer. This is what lets a manual refresh
    stand in when a repo has no webhook.
    """
    __tablename__ = "event_cursor"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_name: Mapped[str] = mapped_column(String(200), index=True)
    repo_url: Mapped[str] = mapped_column(String(500))        # normalized VCS url
    last_seen_at: Mapped[str | None] = mapped_column(String(40), nullable=True)   # ISO8601
    last_seen_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("project_name", "repo_url", name="uq_cursor_project_repo"),)


class RenderedEvent(Base):
    """Materialized cache of a project's RENDERED (triaged) commit events.

    NOT a source of truth: it is fully rebuildable from the durable event feed + the
    triage cache by a replay. It exists so the dashboard renders INSTANTLY at startup
    (load these rows — no per-event triage round-trip) and so a replay can reconcile in
    the BACKGROUND (mark every row pending, re-render the feed upserting fresh rows, then
    sweep the still-pending stale rows) while reads keep serving the previous render.

    Keyed by (project, commit); `payload` is the full result dict the monitor renders
    from. `pending` drives the mark-and-sweep. Cleared by reset_db / recreate --reset-feed.
    """
    __tablename__ = "rendered_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_name: Mapped[str] = mapped_column(String(200), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64))
    component: Mapped[str | None] = mapped_column(String(200), nullable=True)
    committed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)  # ISO, ordering
    label: Mapped[str | None] = mapped_column(String(40), nullable=True)         # _event_label
    pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("project_name", "commit_sha",
                                       name="uq_rendered_project_commit"),)
