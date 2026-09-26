"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from . import attestation_services, services
from .actors import Actor, get_actor
from .db import get_db
from .schemas import (
    AttestationApplyIn,
    AttestationApproveIn,
    AttestationOut,
    AttestationRejectIn,
    AttestationRevokeIn,
    AttestationVerifyIn,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def _attestation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, attestation_services.AttestationNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, attestation_services.PermissionDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, attestation_services.InvalidStateError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 学时证明（attestation）
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/attestations",
    response_model=AttestationOut,
    status_code=status.HTTP_201_CREATED,
)
def apply_attestation(
    plan_version: str,
    body: AttestationApplyIn,
    response: Response,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        result, created = attestation_services.apply(
            db,
            actor,
            plan_version=plan_version,
            freeze_id=body.freeze_id,
            student_id=body.student_id,
            purpose=body.purpose,
            request_id=body.request_id,
        )
    except attestation_services.AttestationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        attestation_services.PermissionDeniedError,
        attestation_services.ValidationError,
    ) as exc:
        raise _attestation_error(exc) from exc
    # 幂等重复请求返回 200 而非 201。
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/plans/{plan_version}/students/{student_id}/attestations",
    response_model=list[AttestationOut],
)
def list_attestations(
    plan_version: str,
    student_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        return attestation_services.list_for_student(db, actor, plan_version, student_id)
    except attestation_services.PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/attestations/{attestation_id}", response_model=AttestationOut)
def get_attestation(
    attestation_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        return attestation_services.get_metadata(db, actor, attestation_id)
    except attestation_services.AttestationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except attestation_services.PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post(
    "/attestations/{attestation_id}/approve",
    response_model=AttestationOut,
    status_code=status.HTTP_201_CREATED,
)
def approve_attestation(
    attestation_id: str,
    body: AttestationApproveIn,
    response: Response,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        result, issued = attestation_services.approve(
            db,
            actor,
            attestation_id,
            valid_for_days=body.valid_for_days,
            expires_at=body.expires_at,
            valid_from=body.valid_from,
            supersedes_id=body.supersedes_id,
            note=body.note,
        )
    except (
        attestation_services.AttestationNotFoundError,
        attestation_services.PermissionDeniedError,
        attestation_services.InvalidStateError,
        attestation_services.ValidationError,
    ) as exc:
        raise _attestation_error(exc) from exc
    if not issued:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/attestations/{attestation_id}/reject",
    response_model=AttestationOut,
    status_code=status.HTTP_200_OK,
)
def reject_attestation(
    attestation_id: str,
    body: AttestationRejectIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        result, _ = attestation_services.reject(
            db, actor, attestation_id, note=body.note
        )
    except (
        attestation_services.AttestationNotFoundError,
        attestation_services.PermissionDeniedError,
        attestation_services.InvalidStateError,
        attestation_services.ValidationError,
    ) as exc:
        raise _attestation_error(exc) from exc
    return result


@router.post(
    "/attestations/{attestation_id}/revoke",
    response_model=AttestationOut,
)
def revoke_attestation(
    attestation_id: str,
    body: AttestationRevokeIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        result, _ = attestation_services.revoke(
            db, actor, attestation_id, reason=body.reason
        )
    except (
        attestation_services.AttestationNotFoundError,
        attestation_services.PermissionDeniedError,
        attestation_services.InvalidStateError,
        attestation_services.ValidationError,
    ) as exc:
        raise _attestation_error(exc) from exc
    return result


@router.get("/attestations/{attestation_id}/package")
def download_attestation_package(
    attestation_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        return attestation_services.download(db, actor, attestation_id)
    except attestation_services.AttestationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except attestation_services.PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except attestation_services.InvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/attestations/verify")
def verify_attestation(
    body: AttestationVerifyIn,
    db: Session = Depends(get_db),
) -> Any:
    # 离线核验不要求身份；任何持有数据包的一方（如用人单位）都可校验。
    return attestation_services.verify_offline(db, body.package)


@router.get("/attestations/{attestation_id}/self-check")
def attestation_self_check(
    attestation_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> Any:
    try:
        # 自检会暴露内部一致性细节，限导师/管理员。
        if not (actor.is_admin or actor.is_mentor):
            raise attestation_services.PermissionDeniedError("只有导师或管理员可以执行自检")
        return attestation_services.restart_self_check(db, attestation_id)
    except attestation_services.AttestationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except attestation_services.PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
