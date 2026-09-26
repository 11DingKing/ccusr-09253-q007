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


class Certificate(Base):
    """学时证明申请及其生命周期状态。

    数据包在批准（签发）时一次性生成并冻结在 ``package`` 中；之后本行只允许
    状态与撤销信息发生变化，原数据包永不修改。纠错只能新申请一张替代证明，
    通过 ``supersedes_id`` 链接旧编号。
    """

    __tablename__ = "certificates"

    certificate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    freeze_id: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(256), nullable=False)
    applicant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    package: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    approver_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','issued','rejected','revoked','superseded')",
            name="ck_certificates_status",
        ),
        # 幂等键：同一申请人、同一学生、同一冻结快照、同一用途只允许一份申请。
        UniqueConstraint(
            "applicant_id",
            "student_id",
            "plan_version",
            "freeze_id",
            "purpose",
            name="uq_certificates_request",
        ),
        Index("ix_certificates_student", "plan_version", "student_id"),
        Index("ix_certificates_supersedes", "supersedes_id"),
    )


class CertificateAudit(Base):
    """证明生命周期审计：申请、批准、驳回、撤销、替代均留痕。"""

    __tablename__ = "certificate_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    certificate_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "certificate_id", "sequence", name="uq_certificate_audit_sequence"
        ),
    )
