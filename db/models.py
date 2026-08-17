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

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
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
