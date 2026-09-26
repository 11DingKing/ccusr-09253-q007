"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Certificate, CertificateAudit
from .models import Event as EventModel
from .models import Freeze, Plan


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 学时证明
# ---------------------------------------------------------------------------


def find_certificate_by_request(
    db: Session,
    *,
    applicant_id: str,
    student_id: str,
    plan_version: str,
    freeze_id: str,
    purpose: str,
) -> Certificate | None:
    stmt = select(Certificate).where(
        Certificate.applicant_id == applicant_id,
        Certificate.student_id == student_id,
        Certificate.plan_version == plan_version,
        Certificate.freeze_id == freeze_id,
        Certificate.purpose == purpose,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_certificate(db: Session, certificate_id: str) -> Certificate | None:
    return db.get(Certificate, certificate_id)


def list_certificates(
    db: Session,
    *,
    student_id: str | None = None,
    applicant_id: str | None = None,
) -> list[Certificate]:
    stmt = select(Certificate).order_by(Certificate.created_at)
    if student_id is not None:
        stmt = stmt.where(Certificate.student_id == student_id)
    if applicant_id is not None:
        stmt = stmt.where(Certificate.applicant_id == applicant_id)
    return list(db.execute(stmt).scalars().all())


def insert_certificate(db: Session, **values: Any) -> Certificate | None:
    """插入证明申请；命中幂等唯一键或主键冲突时返回 None（由调用方回读）。

    不提交事务，由调用方与审计条目一起提交。
    """
    stmt = sqlite_insert(Certificate).values(**values)
    stmt = stmt.on_conflict_do_nothing().returning(Certificate.certificate_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    return db.get(Certificate, inserted)


def update_certificate_status(
    db: Session,
    certificate_id: str,
    *,
    expected_status: str,
    changes: dict[str, Any],
) -> int:
    """比较并设置状态：仅当当前状态等于 ``expected_status`` 时生效。

    不提交事务，由调用方与审计写入一起提交。返回受影响行数（0 表示状态已被
    其他请求抢先变更）。
    """
    statement = (
        sa_update(Certificate)
        .where(Certificate.certificate_id == certificate_id)
        .where(Certificate.status == expected_status)
        .values(**changes)
    )
    return db.execute(statement).rowcount


def mark_certificate_superseded(
    db: Session, old_certificate_id: str, new_certificate_id: str, now: datetime
) -> int:
    """把仍然有效的旧证明标记为 superseded（已撤销的不复活）。不提交。"""
    statement = (
        sa_update(Certificate)
        .where(Certificate.certificate_id == old_certificate_id)
        .where(Certificate.status == "issued")
        .values(status="superseded", updated_at=now)
    )
    return db.execute(statement).rowcount


def add_certificate_audit(
    db: Session,
    *,
    certificate_id: str,
    action: str,
    actor_id: str,
    detail: str = "",
) -> None:
    """在当前事务内追加审计条目（序号取行级最大值，调用方负责提交）。"""
    next_seq = (
        db.execute(
            select(sa_func.coalesce(sa_func.max(CertificateAudit.sequence), 0)).where(
                CertificateAudit.certificate_id == certificate_id
            )
        ).scalar_one()
        + 1
    )
    db.add(
        CertificateAudit(
            certificate_id=certificate_id,
            sequence=next_seq,
            action=action,
            actor_id=actor_id,
            detail=detail[:512],
        )
    )


def list_certificate_audit(db: Session, certificate_id: str) -> list[CertificateAudit]:
    stmt = (
        select(CertificateAudit)
        .where(CertificateAudit.certificate_id == certificate_id)
        .order_by(CertificateAudit.sequence)
    )
    return list(db.execute(stmt).scalars().all())
