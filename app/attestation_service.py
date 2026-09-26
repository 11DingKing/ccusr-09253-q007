"""学时证明的申请、审批、签发、撤销与离线核验编排。

约束要点：

* 证明只能从**冻结快照**中的学生条目派生；事件表在冻结之后的变化不影响证明。
* 签发时一次性生成最小披露数据包并存入 ``certificates.package``，此后该 JSON
  永不更新；纠错只能新申请替代证明（``supersedes_id`` 链接旧编号），旧证明在
  替代证明签发时被标记为 ``superseded``。
* 所有状态迁移使用“比较当前状态再更新”的条件 UPDATE，并与审计条目在同一事务
  提交，保证并发审批/撤销时只有一个请求生效。
* 申请通过 (申请人, 学生, 方案, 冻结, 用途) 唯一键幂等。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core import attestations as att
from .core.snapshot import Snapshot
from .models import Certificate
from .repository import (
    add_certificate_audit,
    find_certificate_by_request,
    get_certificate,
    get_freeze,
    insert_certificate,
    list_certificate_audit,
    list_certificates,
    load_events_up_to,
    mark_certificate_superseded,
    update_certificate_status,
)

STATUS_PENDING = "pending"
STATUS_ISSUED = "issued"
STATUS_REJECTED = "rejected"
STATUS_REVOKED = "revoked"
STATUS_SUPERSEDED = "superseded"

# 已签发、已替代或已撤销的证明都可以作为纠错替代链的链接目标；其中已撤销的
# 证明保持其终态，不会被改标为 superseded。
_REPLACEABLE_STATUSES = {
    STATUS_ISSUED,
    STATUS_SUPERSEDED,
    STATUS_REVOKED,
}


class CertificateNotFoundError(Exception):
    pass


class CertificateStateError(Exception):
    def __init__(self, message: str, *, current_status: str | None = None) -> None:
        super().__init__(message)
        self.current_status = current_status


class RequestConflictError(Exception):
    """请求与既有申请冲突（幂等键相同但证明编号不同）。"""


class InvalidRequestError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 经 SQLAlchemy 读回的时间可能是朴素时间，按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    instant = _as_utc(value)
    return instant.isoformat().replace("+00:00", "Z") if instant else None


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def serialize_certificate(row: Certificate, *, now: datetime | None = None) -> dict[str, Any]:
    instant = _as_utc(now) or _now()
    status = row.status
    expired = (
        status == STATUS_ISSUED
        and row.expires_at is not None
        and _as_utc(row.expires_at) <= instant
    )
    return {
        "certificate_id": row.certificate_id,
        "plan_version": row.plan_version,
        "freeze_id": row.freeze_id,
        "student_id": row.student_id,
        "purpose": row.purpose,
        "applicant_id": row.applicant_id,
        "status": status,
        "supersedes_id": row.supersedes_id,
        "created_at": _iso(row.created_at),
        "issued_at": _iso(row.issued_at),
        "expires_at": _iso(row.expires_at),
        "approver_id": row.approver_id,
        "decision_reason": row.decision_reason,
        "revoked_at": _iso(row.revoked_at),
        "revoked_reason": row.revoked_reason,
        "expired": expired,
    }


def serialize_audit(rows) -> list[dict[str, Any]]:
    return [
        {
            "sequence": r.sequence,
            "action": r.action,
            "actor_id": r.actor_id,
            "detail": r.detail,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 冻结快照读取
# ---------------------------------------------------------------------------


def _load_freeze_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> tuple[Snapshot, dict[str, Any]]:
    freeze = get_freeze(db, plan_version, freeze_id)
    if freeze is None:
        raise CertificateNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    snapshot = Snapshot.from_dict(freeze.snapshot)
    student = next(
        (s for s in snapshot.students if s["student_id"] == student_id), None
    )
    if student is None:
        raise CertificateNotFoundError(
            f"student '{student_id}' is not present in freeze '{freeze_id}'"
        )
    return snapshot, student


# ---------------------------------------------------------------------------
# 申请
# ---------------------------------------------------------------------------


def request_certificate(
    db: Session,
    *,
    certificate_id: str,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    purpose: str,
    applicant_id: str,
    expires_at: datetime | None = None,
    ttl_days: int | None = None,
    supersedes_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """创建证明申请。返回 (证明元数据, 是否新建)。重复请求幂等。"""
    purpose = purpose.strip()
    if not purpose:
        raise InvalidRequestError("purpose must not be empty")
    if expires_at is not None and ttl_days is not None:
        raise InvalidRequestError("provide either expires_at or ttl_days, not both")
    if ttl_days is not None and ttl_days <= 0:
        raise InvalidRequestError("ttl_days must be positive")
    if expires_at is not None and expires_at.tzinfo is None:
        raise InvalidRequestError("expires_at must be timezone-aware")

    # 冻结快照与学生必须存在；此处只读取，不修改快照。
    _load_freeze_student(db, plan_version, freeze_id, student_id)

    if supersedes_id is not None:
        old = get_certificate(db, supersedes_id)
        if old is None:
            raise CertificateNotFoundError(
                f"superseded certificate '{supersedes_id}' does not exist"
            )
        if (old.plan_version, old.student_id) != (plan_version, student_id):
            raise InvalidRequestError(
                "a replacement must target the same plan and student as the old certificate"
            )
        if old.status not in _REPLACEABLE_STATUSES:
            raise CertificateStateError(
                f"certificate in status '{old.status}' cannot be superseded",
                current_status=old.status,
            )

    final_expires: datetime | None = None
    if expires_at is not None:
        final_expires = expires_at.astimezone(timezone.utc)
    elif ttl_days is not None:
        final_expires = _now() + timedelta(days=ttl_days)

    existing = find_certificate_by_request(
        db,
        applicant_id=applicant_id,
        student_id=student_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        purpose=purpose,
    )
    if existing is not None:
        if existing.certificate_id != certificate_id:
            raise RequestConflictError(
                "an identical request already exists as certificate "
                f"'{existing.certificate_id}'"
            )
        return serialize_certificate(existing), False

    row = insert_certificate(
        db,
        certificate_id=certificate_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        student_id=student_id,
        purpose=purpose,
        applicant_id=applicant_id,
        status=STATUS_PENDING,
        expires_at=final_expires,
        supersedes_id=supersedes_id,
        package=None,
        source_fingerprint=None,
    )
    if row is None:
        # 并发插入：可能是同编号已存在，也可能是幂等键被占用。
        same_id = get_certificate(db, certificate_id)
        if same_id is not None:
            if (
                same_id.applicant_id == applicant_id
                and same_id.student_id == student_id
                and same_id.plan_version == plan_version
                and same_id.freeze_id == freeze_id
                and same_id.purpose == purpose
            ):
                return serialize_certificate(same_id), False
            raise RequestConflictError(
                f"certificate id '{certificate_id}' is already used by another request"
            )
        raced = find_certificate_by_request(
            db,
            applicant_id=applicant_id,
            student_id=student_id,
            plan_version=plan_version,
            freeze_id=freeze_id,
            purpose=purpose,
        )
        if raced is not None:
            if raced.certificate_id != certificate_id:
                raise RequestConflictError(
                    "an identical request already exists as certificate "
                    f"'{raced.certificate_id}'"
                )
            return serialize_certificate(raced), False
        raise InvalidRequestError("certificate could not be created")

    add_certificate_audit(
        db,
        certificate_id=certificate_id,
        action="request",
        actor_id=applicant_id,
        detail=f"purpose={purpose}; freeze={freeze_id}",
    )
    db.commit()
    db.refresh(row)
    return serialize_certificate(row), True


# ---------------------------------------------------------------------------
# 审批 / 驳回（签发）
# ---------------------------------------------------------------------------


def _build_package_for(
    db: Session, row: Certificate, *, issued_at: datetime
) -> tuple[dict[str, Any], str]:
    snapshot, student = _load_freeze_student(
        db, row.plan_version, row.freeze_id, row.student_id
    )
    cutoff_events = (
        load_events_up_to(
            db, row.plan_version, snapshot.event_cutoff_id
        )
        if snapshot.event_cutoff_id is not None
        else []
    )
    chain = att.build_confirmation_chain(student, cutoff_events)
    claims = att.build_claims(
        timezone_name=snapshot.timezone,
        required_seconds=snapshot.required_seconds,
        student=student,
        confirmation_chain=chain,
    )
    fingerprint = att.build_source_fingerprint(
        plan_version=row.plan_version,
        freeze_id=row.freeze_id,
        freeze_generated_at=snapshot.generated_at,
        event_cutoff_id=snapshot.event_cutoff_id,
        student=student,
    )
    package = att.build_package(
        certificate_id=row.certificate_id,
        student_id=row.student_id,
        plan_version=row.plan_version,
        purpose=row.purpose,
        freeze_id=row.freeze_id,
        freeze_generated_at=snapshot.generated_at,
        event_cutoff_id=snapshot.event_cutoff_id,
        claims=claims,
        source_fingerprint=fingerprint,
        issued_at=issued_at,
        expires_at=_as_utc(row.expires_at),
        supersedes_id=row.supersedes_id,
    )
    return package, fingerprint


def approve_certificate(
    db: Session,
    *,
    certificate_id: str,
    approver_id: str,
    reason: str = "",
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """批准并签发证明：冻结数据包入档，旧证明标记为已替代。"""
    instant = _as_utc(now) or _now()
    row = get_certificate(db, certificate_id)
    if row is None:
        raise CertificateNotFoundError(f"certificate '{certificate_id}' does not exist")
    if row.status == STATUS_ISSUED:
        # 重复审批幂等：已签发则原样返回，不重新生成数据包。
        return serialize_certificate(row, now=instant), False
    if row.status != STATUS_PENDING:
        raise CertificateStateError(
            f"only pending certificates can be approved (current='{row.status}')",
            current_status=row.status,
        )

    package, fingerprint = _build_package_for(db, row, issued_at=instant)

    changed = update_certificate_status(
        db,
        certificate_id,
        expected_status=STATUS_PENDING,
        changes={
            "status": STATUS_ISSUED,
            "package": package,
            "source_fingerprint": fingerprint,
            "approver_id": approver_id,
            "decision_reason": reason[:512] or None,
            "issued_at": instant,
            "updated_at": instant,
        },
    )
    if changed == 0:
        db.rollback()
        refreshed = get_certificate(db, certificate_id)
        assert refreshed is not None
        if refreshed.status == STATUS_ISSUED:
            return serialize_certificate(refreshed, now=instant), False
        raise CertificateStateError(
            f"certificate changed to '{refreshed.status}' before approval",
            current_status=refreshed.status,
        )

    add_certificate_audit(
        db,
        certificate_id=certificate_id,
        action="approve",
        actor_id=approver_id,
        detail=reason[:512],
    )

    if row.supersedes_id:
        marked = mark_certificate_superseded(
            db, row.supersedes_id, certificate_id, instant
        )
        if marked:
            add_certificate_audit(
                db,
                certificate_id=row.supersedes_id,
                action="superseded",
                actor_id=approver_id,
                detail=f"replaced_by={certificate_id}",
            )

    db.commit()
    db.refresh(row)
    return serialize_certificate(row, now=instant), True


def reject_certificate(
    db: Session,
    *,
    certificate_id: str,
    approver_id: str,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    if not reason or not reason.strip():
        raise InvalidRequestError("rejection reason is required")
    row = get_certificate(db, certificate_id)
    if row is None:
        raise CertificateNotFoundError(f"certificate '{certificate_id}' does not exist")
    if row.status == STATUS_REJECTED:
        return serialize_certificate(row), False
    if row.status != STATUS_PENDING:
        raise CertificateStateError(
            f"only pending certificates can be rejected (current='{row.status}')",
            current_status=row.status,
        )
    changed = update_certificate_status(
        db,
        certificate_id,
        expected_status=STATUS_PENDING,
        changes={
            "status": STATUS_REJECTED,
            "approver_id": approver_id,
            "decision_reason": reason.strip()[:512],
            "updated_at": _now(),
        },
    )
    if changed == 0:
        db.rollback()
        refreshed = get_certificate(db, certificate_id)
        assert refreshed is not None
        if refreshed.status == STATUS_REJECTED:
            return serialize_certificate(refreshed), False
        raise CertificateStateError(
            f"certificate changed to '{refreshed.status}' before rejection",
            current_status=refreshed.status,
        )
    add_certificate_audit(
        db,
        certificate_id=certificate_id,
        action="reject",
        actor_id=approver_id,
        detail=reason.strip()[:512],
    )
    db.commit()
    db.refresh(row)
    return serialize_certificate(row), True


# ---------------------------------------------------------------------------
# 下载 / 撤销
# ---------------------------------------------------------------------------


def download_package(db: Session, certificate_id: str) -> dict[str, Any]:
    row = get_certificate(db, certificate_id)
    if row is None:
        raise CertificateNotFoundError(f"certificate '{certificate_id}' does not exist")
    if row.package is None:
        raise CertificateStateError(
            f"certificate is '{row.status}': no signed package available",
            current_status=row.status,
        )
    # 已撤销/已替代的证明其原始数据包仍可下载用于追溯，但由状态决定核验结论。
    return dict(row.package)


def revoke_certificate(
    db: Session,
    *,
    certificate_id: str,
    approver_id: str,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    if not reason or not reason.strip():
        raise InvalidRequestError("revocation reason is required")
    row = get_certificate(db, certificate_id)
    if row is None:
        raise CertificateNotFoundError(f"certificate '{certificate_id}' does not exist")
    if row.status == STATUS_REVOKED:
        # 重复撤销幂等。
        return serialize_certificate(row), False
    if row.status != STATUS_ISSUED:
        raise CertificateStateError(
            f"only issued certificates can be revoked (current='{row.status}')",
            current_status=row.status,
        )
    instant = _now()
    changed = update_certificate_status(
        db,
        certificate_id,
        expected_status=STATUS_ISSUED,
        changes={
            "status": STATUS_REVOKED,
            "revoked_at": instant,
            "revoked_reason": reason.strip()[:512],
            "updated_at": instant,
        },
    )
    if changed == 0:
        db.rollback()
        refreshed = get_certificate(db, certificate_id)
        assert refreshed is not None
        if refreshed.status == STATUS_REVOKED:
            return serialize_certificate(refreshed), False
        raise CertificateStateError(
            f"certificate changed to '{refreshed.status}' before revocation",
            current_status=refreshed.status,
        )
    add_certificate_audit(
        db,
        certificate_id=certificate_id,
        action="revoke",
        actor_id=approver_id,
        detail=reason.strip()[:512],
    )
    db.commit()
    db.refresh(row)
    return serialize_certificate(row), True


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------


def _recompute_source_fingerprint(
    db: Session, package: dict[str, Any]
) -> str | None:
    freeze_block = package.get("freeze") or {}
    try:
        snapshot, student = _load_freeze_student(
            db,
            str(package["plan_version"]),
            str(freeze_block["freeze_id"]),
            str(package["student_id"]),
        )
    except CertificateNotFoundError:
        return None
    return att.build_source_fingerprint(
        plan_version=str(package["plan_version"]),
        freeze_id=str(freeze_block["freeze_id"]),
        freeze_generated_at=snapshot.generated_at,
        event_cutoff_id=snapshot.event_cutoff_id,
        student=student,
    )


def verify_certificate(
    db: Session,
    *,
    certificate_id: str | None = None,
    package: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """核验证明。

    * 在线（仅给 ``certificate_id``）：读取服务端存档的数据包与登记状态，并由
      冻结快照重算来源指纹；
    * 离线（给 ``package``）：以上传的数据包为准，能在本地登记中找到同编号记录
      时附带登记状态，找不到也不影响结构/校验码/有效期核验。
    """
    instant = _as_utc(now) or _now()
    target_package = package
    registry_status: str | None = None
    effective_id = certificate_id

    if target_package is None:
        if certificate_id is None:
            raise InvalidRequestError("certificate_id or package is required")
        row = get_certificate(db, certificate_id)
        if row is None:
            raise CertificateNotFoundError(
                f"certificate '{certificate_id}' does not exist"
            )
        if row.package is None:
            raise CertificateStateError(
                f"certificate is '{row.status}': no signed package to verify",
                current_status=row.status,
            )
        target_package = dict(row.package)
        registry_status = row.status
    else:
        effective_id = target_package.get("certificate_id") or certificate_id
        if effective_id:
            row = get_certificate(db, str(effective_id))
            if row is not None:
                registry_status = row.status

    source_fingerprint = _recompute_source_fingerprint(db, target_package)
    result = att.verify_package(
        target_package,
        now=instant,
        registry_status=registry_status,
        source_fingerprint=source_fingerprint,
    )
    expires_at = target_package.get("expires_at")
    result["expires_at"] = expires_at
    if registry_status is not None:
        result["registry_status"] = registry_status
    return result


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_certificate_view(
    db: Session, certificate_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = get_certificate(db, certificate_id)
    if row is None:
        raise CertificateNotFoundError(f"certificate '{certificate_id}' does not exist")
    audit = list_certificate_audit(db, certificate_id)
    return serialize_certificate(row), serialize_audit(audit)


def list_certificate_views(
    db: Session, *, student_id: str | None = None, applicant_id: str | None = None
) -> list[dict[str, Any]]:
    rows = list_certificates(db, student_id=student_id, applicant_id=applicant_id)
    return [serialize_certificate(r) for r in rows]
