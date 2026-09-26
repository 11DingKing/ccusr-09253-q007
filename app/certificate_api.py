"""学时证明申请、审批、下载、撤销与离线核验接口。

调用方身份通过请求头传递：``X-Actor-Id`` 与 ``X-Actor-Role``
（``student`` / ``mentor`` / ``admin``）。权限范围：

* 申请：学员只能为本人申请；导师/管理员可代任意学员申请；
* 批准/驳回：仅导师、管理员；
* 撤销：仅管理员；
* 下载：申请人、学员本人、导师或管理员；
* 核验：任何调用方均可（支持外部离线核验），登记状态只在服务端能匹配编号时附带。
"""

from __future__ import annotations

from typing import Any, NamedTuple

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import attestation_service as svc
from .db import get_db
from .schemas import (
    CertificateDecisionIn,
    CertificateDetailOut,
    CertificateOut,
    CertificateRejectIn,
    CertificateRequestIn,
    CertificateRevokeIn,
    VerifyIn,
    VerifyOut,
)

router = APIRouter(prefix="/api")

ROLE_STUDENT = "student"
ROLE_MENTOR = "mentor"
ROLE_ADMIN = "admin"
_ALL_ROLES = {ROLE_STUDENT, ROLE_MENTOR, ROLE_ADMIN}


class Actor(NamedTuple):
    actor_id: str
    role: str


def require_actor(*roles: str):
    allowed = set(roles) if roles else _ALL_ROLES

    def dependency(
        x_actor_id: str | None = Header(default=None),
        x_actor_role: str = Header(default=ROLE_STUDENT),
    ) -> Actor:
        if not x_actor_id:
            raise HTTPException(status_code=401, detail="X-Actor-Id header is required")
        if x_actor_role not in _ALL_ROLES:
            raise HTTPException(status_code=403, detail="unknown actor role")
        if x_actor_role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role '{x_actor_role}' may not perform this action",
            )
        return Actor(actor_id=x_actor_id, role=x_actor_role)

    return dependency


def _ensure_self(actor: Actor, value: str, message: str) -> None:
    if value != actor.actor_id:
        raise HTTPException(status_code=403, detail=message)


# ---------------------------------------------------------------------------
# 申请
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}/certificates/{certificate_id}",
    response_model=CertificateOut,
)
def request_certificate(
    plan_version: str,
    freeze_id: str,
    certificate_id: str,
    body: CertificateRequestIn,
    response: Response,
    actor: Actor = Depends(require_actor()),
    db: Session = Depends(get_db),
) -> Any:
    # 学员只能为本人申请；导师/管理员可以代其他学员申请。
    _ensure_self(actor, body.applicant_id, "applicant_id must match the acting user")
    if actor.role == ROLE_STUDENT and body.student_id != actor.actor_id:
        raise HTTPException(
            status_code=403,
            detail="students may only request certificates for themselves",
        )
    try:
        view, created = svc.request_certificate(
            db,
            certificate_id=certificate_id,
            plan_version=plan_version,
            freeze_id=freeze_id,
            student_id=body.student_id,
            purpose=body.purpose,
            applicant_id=body.applicant_id,
            expires_at=body.expires_at,
            ttl_days=body.ttl_days,
            supersedes_id=body.supersedes_id,
        )
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.RequestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.InvalidRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return view


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/certificates",
    response_model=list[CertificateOut],
)
def list_freeze_certificates(
    plan_version: str,
    freeze_id: str,
    student_id: str | None = None,
    actor: Actor = Depends(require_actor()),
    db: Session = Depends(get_db),
) -> Any:
    if actor.role == ROLE_STUDENT:
        # 学员只能看到与本人相关（本人是学员或申请人）的证明。
        rows = svc.list_certificate_views(db)
        return [
            v
            for v in rows
            if v["plan_version"] == plan_version
            and v["freeze_id"] == freeze_id
            and actor.actor_id in (v["student_id"], v["applicant_id"])
            and (student_id is None or v["student_id"] == student_id)
        ]
    rows = svc.list_certificate_views(db, student_id=student_id)
    return [
        v
        for v in rows
        if v["plan_version"] == plan_version and v["freeze_id"] == freeze_id
    ]


# ---------------------------------------------------------------------------
# 审批 / 驳回 / 撤销
# ---------------------------------------------------------------------------


@router.post(
    "/certificates/{certificate_id}/approve",
    response_model=CertificateOut,
)
def approve_certificate(
    certificate_id: str,
    body: CertificateDecisionIn,
    actor: Actor = Depends(require_actor(ROLE_MENTOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
) -> Any:
    _ensure_self(actor, body.approver_id, "approver_id must match the acting user")
    try:
        view, _created = svc.approve_certificate(
            db,
            certificate_id=certificate_id,
            approver_id=body.approver_id,
            reason=body.reason,
        )
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return view


@router.post(
    "/certificates/{certificate_id}/reject",
    response_model=CertificateOut,
)
def reject_certificate(
    certificate_id: str,
    body: CertificateRejectIn,
    actor: Actor = Depends(require_actor(ROLE_MENTOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
) -> Any:
    _ensure_self(actor, body.approver_id, "approver_id must match the acting user")
    try:
        view, _created = svc.reject_certificate(
            db,
            certificate_id=certificate_id,
            approver_id=body.approver_id,
            reason=body.reason,
        )
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.InvalidRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return view


@router.post(
    "/certificates/{certificate_id}/revoke",
    response_model=CertificateOut,
)
def revoke_certificate(
    certificate_id: str,
    body: CertificateRevokeIn,
    actor: Actor = Depends(require_actor(ROLE_ADMIN)),
    db: Session = Depends(get_db),
) -> Any:
    _ensure_self(actor, body.approver_id, "approver_id must match the acting user")
    try:
        view, _created = svc.revoke_certificate(
            db,
            certificate_id=certificate_id,
            approver_id=body.approver_id,
            reason=body.reason,
        )
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.InvalidRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return view


# ---------------------------------------------------------------------------
# 查询 / 下载
# ---------------------------------------------------------------------------


@router.get(
    "/certificates/{certificate_id}",
    response_model=CertificateDetailOut,
)
def get_certificate(
    certificate_id: str,
    actor: Actor = Depends(require_actor()),
    db: Session = Depends(get_db),
) -> Any:
    try:
        view, audit = svc.get_certificate_view(db, certificate_id)
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if actor.role == ROLE_STUDENT and actor.actor_id not in (
        view["student_id"],
        view["applicant_id"],
    ):
        raise HTTPException(
            status_code=403, detail="may not view another user's certificate"
        )
    view["audit"] = audit
    return view


@router.get("/certificates/{certificate_id}/download")
def download_certificate(
    certificate_id: str,
    actor: Actor = Depends(require_actor()),
    db: Session = Depends(get_db),
) -> Any:
    try:
        view, _audit = svc.get_certificate_view(db, certificate_id)
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if actor.role == ROLE_STUDENT and actor.actor_id not in (
        view["student_id"],
        view["applicant_id"],
    ):
        raise HTTPException(
            status_code=403, detail="may not download another user's certificate"
        )
    try:
        package = svc.download_package(db, certificate_id)
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    filename = f"certificate-{certificate_id}.json"
    return JSONResponse(
        package,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Certificate-Id": certificate_id,
            "X-Certificate-Status": view["status"],
        },
    )


# ---------------------------------------------------------------------------
# 核验（在线按编号 / 离线按上传数据包）
# ---------------------------------------------------------------------------


@router.post("/certificates/verify", response_model=VerifyOut)
def verify_certificate(
    body: VerifyIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return svc.verify_certificate(
            db, certificate_id=body.certificate_id, package=body.package
        )
    except svc.CertificateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.CertificateStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.InvalidRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
