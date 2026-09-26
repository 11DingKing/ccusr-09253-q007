"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class Attestation(Base):
    """学时证明：申请记录与签发后的不可变数据包。"""

    __tablename__ = "attestations"

    attestation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    freeze_id: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # 签发后写入；package 一经写入永不变更（纠错只能另发替代证明）。
    package: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 替代链：本证明替代哪一份旧证明；旧证明进入 superseded 终态。
    supersedes_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_attestations_request_id"),
        Index(
            "ix_attestations_plan_freeze_student",
            "plan_version",
            "freeze_id",
            "student_id",
        ),
        CheckConstraint(
            "status in ('pending', 'issued', 'rejected', 'revoked', 'superseded')",
            name="ck_attestations_status",
        ),
    )
