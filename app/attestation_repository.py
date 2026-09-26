"""证明（attestation）数据访问层。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import Attestation


def get_by_id(db: Session, attestation_id: str) -> Attestation | None:
    return db.get(Attestation, attestation_id)


def get_by_request_id(db: Session, request_id: str) -> Attestation | None:
    stmt = select(Attestation).where(Attestation.request_id == request_id)
    return db.execute(stmt).scalars().first()


def list_for_student(
    db: Session, plan_version: str, student_id: str
) -> list[Attestation]:
    stmt = (
        select(Attestation)
        .where(Attestation.plan_version == plan_version)
        .where(Attestation.student_id == student_id)
        .order_by(Attestation.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def insert_pending(
    db: Session,
    *,
    attestation_id: str,
    request_id: str,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    purpose: str,
    requested_by: str,
) -> Attestation | None:
    """插入待审批申请；request_id 冲突时返回 None（幂等）。"""
    stmt = sqlite_insert(Attestation).values(
        attestation_id=attestation_id,
        request_id=request_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        student_id=student_id,
        purpose=purpose,
        status="pending",
        requested_by=requested_by,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["request_id"]).returning(
        Attestation.attestation_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(Attestation, attestation_id)


def issue(
    db: Session,
    row: Attestation,
    *,
    package: dict[str, Any],
    checksum: str,
    snapshot_checksum: str,
    issued_by: str,
    valid_from: datetime,
    expires_at: datetime | None,
    issued_at: datetime,
    supersedes_id: str | None,
) -> bool:
    """以当前状态为条件原子签发；并发竞争失败者返回 False。"""
    stmt = (
        update(Attestation)
        .where(Attestation.attestation_id == row.attestation_id)
        .where(Attestation.status == "pending")
        .values(
            status="issued",
            package=package,
            checksum=checksum,
            snapshot_checksum=snapshot_checksum,
            decided_by=issued_by,
            valid_from=valid_from,
            expires_at=expires_at,
            issued_at=issued_at,
            supersedes_id=supersedes_id,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return (result.rowcount or 0) == 1


def reject(
    db: Session,
    row: Attestation,
    *,
    rejected_by: str,
    note: str,
    rejected_at: datetime,
) -> bool:
    stmt = (
        update(Attestation)
        .where(Attestation.attestation_id == row.attestation_id)
        .where(Attestation.status == "pending")
        .values(
            status="rejected",
            decided_by=rejected_by,
            decision_note=note,
            rejected_at=rejected_at,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return (result.rowcount or 0) == 1


def revoke(
    db: Session,
    row: Attestation,
    *,
    revoked_by: str,
    reason: str,
    revoked_at: datetime,
) -> bool:
    stmt = (
        update(Attestation)
        .where(Attestation.attestation_id == row.attestation_id)
        .where(Attestation.status == "issued")
        .values(
            status="revoked",
            revoke_reason=reason,
            decided_by=revoked_by,
            revoked_at=revoked_at,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return (result.rowcount or 0) == 1


def mark_superseded(db: Session, attestation_id: str) -> bool:
    """把旧证明标记为 superseded；issued 或 revoked 都可被替代。"""
    stmt = (
        update(Attestation)
        .where(Attestation.attestation_id == attestation_id)
        .where(Attestation.status.in_(("issued", "revoked")))
        .values(status="superseded")
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1
