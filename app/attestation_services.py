"""证明申请、审批、签发、撤销与核验的业务流程。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import attestation_repository as repo
from .actors import Actor
from .core import attestation as domain
from .core.snapshot import Snapshot
from .repository import get_freeze, get_plan, load_events_up_to
from .models import Attestation


class AttestationNotFoundError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class ValidationError(Exception):
    pass


def _utc(now: datetime | None = None) -> datetime:
    return (now or domain.utc_now()).astimezone(timezone.utc)


def _derive_id(request_id: str) -> str:
    """由幂等键确定性派生证明编号，重复请求拿到同一编号。"""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16].upper()
    return f"ATT-{digest}"


def _load_freeze_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> tuple[Any, Snapshot]:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise AttestationNotFoundError(f"plan version '{plan_version}' 不存在")
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise AttestationNotFoundError(
            f"plan '{plan_version}' 的冻结快照 '{freeze_id}' 不存在"
        )
    return plan, Snapshot.from_dict(row.snapshot)


def _require_owner_or_staff(actor: Actor, student_id: str) -> None:
    if actor.is_admin or actor.is_mentor:
        return
    if actor.role == "student" and actor.actor_id == student_id:
        return
    raise PermissionDeniedError("无权访问其他学生的证明")


def _require_staff(actor: Actor, action: str) -> None:
    if not (actor.is_admin or actor.is_mentor):
        raise PermissionDeniedError(f"只有导师或管理员可以{action}")


def serialize(row: Attestation) -> dict[str, Any]:
    """证明元数据（不含完整数据包；下载走专门接口）。"""
    return {
        "attestation_id": row.attestation_id,
        "request_id": row.request_id,
        "plan_version": row.plan_version,
        "freeze_id": row.freeze_id,
        "student_id": row.student_id,
        "purpose": row.purpose,
        "status": row.status,
        "requested_by": row.requested_by,
        "decided_by": row.decided_by,
        "decision_note": row.decision_note,
        "valid_from": row.valid_from.isoformat().replace("+00:00", "Z")
        if row.valid_from
        else None,
        "expires_at": row.expires_at.isoformat().replace("+00:00", "Z")
        if row.expires_at
        else None,
        "issued_at": row.issued_at.isoformat().replace("+00:00", "Z")
        if row.issued_at
        else None,
        "revoked_at": row.revoked_at.isoformat().replace("+00:00", "Z")
        if row.revoked_at
        else None,
        "revoke_reason": row.revoke_reason,
        "supersedes_id": row.supersedes_id,
        "checksum": row.checksum,
        "snapshot_checksum": row.snapshot_checksum,
        "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
    }


def apply(
    db: Session,
    actor: Actor,
    *,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    purpose: str,
    request_id: str,
) -> tuple[dict[str, Any], bool]:
    """学生发起证明申请。返回 (证明, 是否本次新建)。重复请求幂等。"""
    purpose = purpose.strip()
    if not purpose:
        raise ValidationError("用途不能为空")
    if not (actor.is_admin or (actor.role == "student" and actor.actor_id == student_id)):
        raise PermissionDeniedError("学生只能为本人申请证明")

    # 幂等命中：直接返回既有申请；request_id 不得跨申请人复用。
    existing = repo.get_by_request_id(db, request_id)
    if existing is not None:
        if existing.requested_by != actor.actor_id:
            raise PermissionDeniedError("幂等键已被其他申请人使用")
        return serialize(existing), False

    plan, snapshot = _load_freeze_snapshot(db, plan_version, freeze_id)
    if not any(s["student_id"] == student_id for s in snapshot.students):
        raise ValidationError(f"冻结快照中不存在学生 '{student_id}'")

    attestation_id = _derive_id(request_id)
    row = repo.insert_pending(
        db,
        attestation_id=attestation_id,
        request_id=request_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        student_id=student_id,
        purpose=purpose,
        requested_by=actor.actor_id,
    )
    if row is None:
        # 并发下另一个请求率先占用了 request_id。
        winner = repo.get_by_request_id(db, request_id)
        assert winner is not None
        return serialize(winner), False
    return serialize(row), True


def _resolve_expiry(
    issued_at: datetime,
    *,
    valid_for_days: int | None,
    expires_at: datetime | None,
) -> datetime | None:
    if expires_at is not None:
        expiry = _utc(expires_at)
        if expiry <= issued_at:
            raise ValidationError("有效期截止时间必须晚于签发时间")
        return expiry
    if valid_for_days is not None:
        if valid_for_days <= 0:
            raise ValidationError("有效天数必须大于零")
        return issued_at + timedelta(days=valid_for_days)
    return None


def approve(
    db: Session,
    actor: Actor,
    attestation_id: str,
    *,
    valid_for_days: int | None = None,
    expires_at: datetime | None = None,
    valid_from: datetime | None = None,
    supersedes_id: str | None = None,
    note: str = "",
) -> tuple[dict[str, Any], bool]:
    """导师/管理员审批并签发。返回 (证明, 是否本次签发)。"""
    _require_staff(actor, "审批证明")
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    if row.status != "pending":
        # 并发/重复审批：幂等返回当前状态，不重新签发。
        return serialize(row), False

    issued_at = _utc()
    effective_from = _utc(valid_from) if valid_from is not None else issued_at
    if effective_from > issued_at + timedelta(days=1):
        raise ValidationError("生效时间不能晚于签发时间超过一天")
    expiry = _resolve_expiry(
        issued_at, valid_for_days=valid_for_days, expires_at=expires_at
    )

    old_row: Attestation | None = None
    if supersedes_id is not None:
        old_row = repo.get_by_id(db, supersedes_id)
        if old_row is None:
            raise ValidationError(f"被替代的证明 '{supersedes_id}' 不存在")
        if old_row.status not in ("issued", "revoked"):
            raise InvalidStateError(
                f"只能替代已签发或已撤销的证明，当前状态为 '{old_row.status}'"
            )
        if old_row.plan_version != row.plan_version:
            raise ValidationError("替代证明必须基于同一培养方案")
        if old_row.student_id != row.student_id:
            raise ValidationError("不能替代其他学生的证明")

    plan, snapshot = _load_freeze_snapshot(db, row.plan_version, row.freeze_id)
    cutoff_events = load_events_up_to(
        db, row.plan_version, snapshot.event_cutoff_id
    ) if snapshot.event_cutoff_id else []

    try:
        package = domain.build_package(
            attestation_id=attestation_id,
            snapshot=snapshot,
            student_id=row.student_id,
            purpose=row.purpose,
            issued_at=issued_at,
            valid_from=effective_from,
            expires_at=expiry,
            events_up_to_cutoff=cutoff_events,
            supersedes_id=supersedes_id,
        )
    except domain.AttestationError as exc:
        raise ValidationError(str(exc)) from exc

    won = repo.issue(
        db,
        row,
        package=package,
        checksum=package["checksum"],
        snapshot_checksum=package["source_checksum"],
        issued_by=actor.actor_id,
        valid_from=effective_from,
        expires_at=expiry,
        issued_at=issued_at,
        supersedes_id=supersedes_id,
    )
    if not won:
        winner = repo.get_by_id(db, attestation_id)
        assert winner is not None
        return serialize(winner), False

    if old_row is not None:
        # 新证明签发成功后才把旧证明移入 superseded；其数据包保持不变。
        repo.mark_superseded(db, old_row.attestation_id)
        db.commit()

    stored = repo.get_by_id(db, attestation_id)
    assert stored is not None
    return serialize(stored), True


def reject(
    db: Session, actor: Actor, attestation_id: str, *, note: str
) -> tuple[dict[str, Any], bool]:
    _require_staff(actor, "审批证明")
    if not note.strip():
        raise ValidationError("驳回必须填写原因")
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    if row.status != "pending":
        return serialize(row), False
    won = repo.reject(
        db, row, rejected_by=actor.actor_id, note=note.strip(), rejected_at=_utc()
    )
    if not won:
        winner = repo.get_by_id(db, attestation_id)
        assert winner is not None
        return serialize(winner), False
    stored = repo.get_by_id(db, attestation_id)
    assert stored is not None
    return serialize(stored), True


def get_metadata(
    db: Session, actor: Actor, attestation_id: str
) -> dict[str, Any]:
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    _require_owner_or_staff(actor, row.student_id)
    return serialize(row)


def download(
    db: Session, actor: Actor, attestation_id: str
) -> dict[str, Any]:
    """下载签发后的不可变最小披露数据包。"""
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    _require_owner_or_staff(actor, row.student_id)
    if row.status == "pending" or row.status == "rejected":
        raise InvalidStateError(f"证明当前状态为 '{row.status}'，无可下载的数据包")
    if row.status == "revoked":
        raise InvalidStateError("证明已被撤销，数据包停止对外提供")
    if row.status == "superseded":
        raise InvalidStateError("证明已被替代证明取代，请使用最新证明编号")
    assert row.package is not None
    return row.package


def revoke(
    db: Session, actor: Actor, attestation_id: str, *, reason: str
) -> tuple[dict[str, Any], bool]:
    """撤销证明。返回 (证明, 是否本次撤销)；重复撤销幂等。"""
    _require_staff(actor, "撤销证明")
    if not reason.strip():
        raise ValidationError("撤销必须填写原因")
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    if row.status == "revoked":
        # 幂等重放：已撤销则原样返回，不覆盖原撤销原因。
        return serialize(row), False
    if row.status != "issued":
        raise InvalidStateError(f"只能撤销已签发的证明，当前状态为 '{row.status}'")
    won = repo.revoke(
        db, row, revoked_by=actor.actor_id, reason=reason.strip(), revoked_at=_utc()
    )
    if not won:
        raise InvalidStateError("撤销失败：证明状态已被其他操作改变")
    stored = repo.get_by_id(db, attestation_id)
    assert stored is not None
    return serialize(stored), True


def list_for_student(
    db: Session, actor: Actor, plan_version: str, student_id: str
) -> list[dict[str, Any]]:
    _require_owner_or_staff(actor, student_id)
    rows = repo.list_for_student(db, plan_version, student_id)
    return [serialize(r) for r in rows]


def verify_offline(
    db: Session, package: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """离线核验：仅凭数据包内容校验；若库中认识该编号则附带在线状态。"""
    report = domain.verify_package(package, now=now)
    attestation_id = report.get("attestation_id")
    online: dict[str, Any] | None = None
    if isinstance(attestation_id, str):
        row = repo.get_by_id(db, attestation_id)
        if row is not None:
            online = {
                "known": True,
                "status": row.status,
                "revoked": row.status == "revoked",
                "revoke_reason": row.revoke_reason,
                "supersedes_id": row.supersedes_id,
                "package_matches_record": row.checksum == package.get("checksum"),
            }
        else:
            online = {"known": False}
    report["online_record"] = online
    if online is not None and online.get("status") in ("revoked", "superseded"):
        report["valid"] = False
        report["errors"].append(
            "证明已被撤销" if online["status"] == "revoked" else "证明已被替代"
        )
    return report


def restart_self_check(db: Session, attestation_id: str) -> dict[str, Any]:
    """重启/灾后自检：重新计算存储的证明与冻结快照是否仍然自洽。

    不依赖签发时缓存的任何派生状态，全部从落库的原始行重算：
    - 数据包校验码与内容一致；
    - 数据包来源摘要与冻结快照行一致（快照未被改动）；
    - 数据包披露量能从同一冻结快照重新派生；
    - 导师确认链可由截止范围内的事件重建。
    """
    row = repo.get_by_id(db, attestation_id)
    if row is None:
        raise AttestationNotFoundError(f"证明 '{attestation_id}' 不存在")
    if row.package is None:
        return {"attestation_id": attestation_id, "status": row.status, "checks": []}

    package = row.package
    checks: dict[str, bool] = {}

    checks["package_checksum"] = (
        domain.checksum_of(
            {k: v for k, v in package.items() if k != "checksum"}
        )
        == package.get("checksum") == row.checksum
    )

    freeze_row = get_freeze(db, row.plan_version, row.freeze_id)
    snapshot = Snapshot.from_dict(freeze_row.snapshot) if freeze_row else None
    checks["freeze_exists"] = freeze_row is not None
    checks["snapshot_binding"] = (
        snapshot is not None and domain.verify_snapshot_binding(package, snapshot)
    )

    if snapshot is not None:
        derived = domain.derive_totals_from_snapshot(snapshot, row.student_id)
        checks["hours_derivable_from_freeze"] = (
            derived is not None and derived == package.get("hours")
        )
        cutoff_events = load_events_up_to(
            db, row.plan_version, snapshot.event_cutoff_id
        ) if snapshot.event_cutoff_id else []
        rebuilt, _ = domain._mentor_confirmation_links(cutoff_events, row.student_id)
        checks["mentor_chain_rebuilds"] = [
            {
                "checkin_event_id": link.checkin_event_id,
                "confirmed_by_event_id": link.confirmed_by_event_id,
            }
            for link in rebuilt
        ] == package.get("mentor_confirmation_chain")

    return {
        "attestation_id": attestation_id,
        "status": row.status,
        "checks": checks,
        "valid": all(checks.values()),
    }
