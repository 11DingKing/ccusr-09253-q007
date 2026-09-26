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


class AttestationApplyIn(BaseModel):
    freeze_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    purpose: str = Field(..., min_length=1, max_length=256)
    request_id: str = Field(..., min_length=1, max_length=128)


class AttestationApproveIn(BaseModel):
    valid_for_days: int | None = Field(default=None, gt=0, le=3650)
    expires_at: datetime | None = None
    valid_from: datetime | None = None
    supersedes_id: str | None = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def _check_expiry_inputs(self) -> "AttestationApproveIn":
        if (
            self.expires_at is not None
            and self.expires_at.tzinfo is None
        ):
            raise ValueError("expires_at 必须带时区")
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            raise ValueError("valid_from 必须带时区")
        return self


class AttestationRejectIn(BaseModel):
    note: str = Field(..., min_length=1, max_length=512)


class AttestationRevokeIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


class AttestationOut(BaseModel):
    attestation_id: str
    request_id: str
    plan_version: str
    freeze_id: str
    student_id: str
    purpose: str
    status: str
    requested_by: str
    decided_by: str | None
    decision_note: str | None
    valid_from: str | None
    expires_at: str | None
    issued_at: str | None
    revoked_at: str | None
    revoke_reason: str | None
    supersedes_id: str | None
    checksum: str | None
    snapshot_checksum: str | None
    created_at: str


class AttestationVerifyIn(BaseModel):
    package: dict[str, Any]
