"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------------------------
# 学时证明
# ---------------------------------------------------------------------------


class CertificateRequestIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    purpose: str = Field(..., min_length=1, max_length=256)
    applicant_id: str = Field(..., min_length=1, max_length=128)
    expires_at: datetime | None = None
    ttl_days: int | None = Field(default=None, gt=0, le=3650)
    supersedes_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check_expiry_inputs(self) -> "CertificateRequestIn":
        if self.expires_at is not None and self.ttl_days is not None:
            raise ValueError("provide either expires_at or ttl_days, not both")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (RFC 3339)")
        return self


class CertificateDecisionIn(BaseModel):
    approver_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="", max_length=512)


class CertificateRejectIn(BaseModel):
    approver_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class CertificateRevokeIn(BaseModel):
    approver_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class CertificateOut(BaseModel):
    certificate_id: str
    plan_version: str
    freeze_id: str
    student_id: str
    purpose: str
    applicant_id: str
    status: str
    supersedes_id: str | None
    created_at: str
    issued_at: str | None
    expires_at: str | None
    approver_id: str | None
    decision_reason: str | None
    revoked_at: str | None
    revoked_reason: str | None
    expired: bool


class CertificateAuditOut(BaseModel):
    sequence: int
    action: str
    actor_id: str
    detail: str
    created_at: str


class CertificateDetailOut(CertificateOut):
    audit: list[CertificateAuditOut] = Field(default_factory=list)


class VerifyIn(BaseModel):
    package: dict[str, Any] | None = None
    certificate_id: str | None = None

    @model_validator(mode="after")
    def _require_one(self) -> "VerifyIn":
        if self.package is None and not self.certificate_id:
            raise ValueError("either package or certificate_id is required")
        return self


class VerifyOut(BaseModel):
    certificate_id: str | None
    valid: bool
    checks: dict[str, Any]
    reasons: list[str]
    expires_at: str | None = None
    registry_status: str | None = None
