"""学时证明领域逻辑：最小披露数据包、校验码与离线核验。

数据包（package）是自包含的：持有方在离线环境下仅凭数据包本身即可
校验：
1. 内容与校验码一致（canonical JSON + SHA-256）；
2. 数据来自所声明的冻结快照（snapshot_checksum）；
3. 导师确认链完整（每条已计入的签到要么无需导师确认，要么能在
   快照截止范围内找到针对该签到的 mentor_confirm 事件）。

撤销状态不写入数据包（数据包一经签发不可变），需通过在线接口查询；
有效期则直接封入数据包，离线可判定是否过期。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .replay import Event, EventType
from .snapshot import Snapshot

# 数据包格式版本，未来不兼容变更时递增。
PACKAGE_FORMAT_VERSION = "1.0"


class AttestationError(ValueError):
    """封装证明领域的业务约束。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """解析数据包中的 ISO-8601 时间戳（允许结尾 Z）。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise AttestationError("时间戳必须包含时区")
    return dt.astimezone(timezone.utc)


def canonical_json(payload: Any) -> str:
    """确定性 JSON 序列化：键排序、无空白、不转义非 ASCII。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def checksum_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MentorLink:
    """一条导师确认链：已确认签到 <- mentor_confirm 事件。"""

    checkin_event_id: str
    confirmed_by_event_id: str


def _mentor_confirmation_links(
    events: list[Event], student_id: str
) -> tuple[list[MentorLink], set[str]]:
    """在快照截止范围内重建导师确认链。

    返回 (确认链, 被确认的签到事件集合)。只认可同一学生名下、且
    confirm 事件 id 不晚于快照截止（events 已按截止过滤）的确认。
    """
    checkins: set[str] = set()
    links: list[MentorLink] = []
    for event in sorted(events, key=lambda e: e.event_id):
        if event.student_id != student_id:
            continue
        if event.event_type == EventType.CHECKIN:
            checkins.add(event.event_id)
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target = event.payload.get("checkin_event_id")
            # 与 replay 一致：只认可事件序上早于本确认事件、且属于
            # 同一学生的签到。
            if (
                isinstance(target, str)
                and target in checkins
                and target < event.event_id
            ):
                links.append(
                    MentorLink(
                        checkin_event_id=target,
                        confirmed_by_event_id=event.event_id,
                    )
                )
    return links, {link.checkin_event_id for link in links}


def _student_in_snapshot(snapshot: Snapshot, student_id: str) -> dict[str, Any]:
    for student in snapshot.students:
        if student["student_id"] == student_id:
            return student
    raise AttestationError(
        f"冻结快照 '{snapshot.freeze_id}' 中不存在学生 '{student_id}'"
    )


def build_package(
    *,
    attestation_id: str,
    snapshot: Snapshot,
    student_id: str,
    purpose: str,
    issued_at: datetime,
    valid_from: datetime,
    expires_at: datetime | None,
    events_up_to_cutoff: list[Event] | None = None,
    supersedes_id: str | None = None,
) -> dict[str, Any]:
    """从冻结快照生成最小披露数据包。

    只包含该学生一人、只披露合规所需的汇总量与导师确认链，不包含
    原始签到明细、其他学生或快照内任何无关字段。
    """
    if expires_at is not None and expires_at <= valid_from:
        raise AttestationError("有效期截止时间必须晚于生效时间")

    student = _student_in_snapshot(snapshot, student_id)

    # 用事件重放确认：快照中所有 CONFIRMED 签到确实有确认链支撑。
    mentor_chain: list[dict[str, str]] = []
    if events_up_to_cutoff is not None:
        links, confirmed_ids = _mentor_confirmation_links(
            events_up_to_cutoff, student_id
        )
        internships = [
            c
            for c in student.get("checkins", [])
            if c.get("activity_type") == "internship"
            and c.get("status") == "CONFIRMED"
        ]
        for checkin in internships:
            if checkin["event_id"] not in confirmed_ids:
                raise AttestationError(
                    f"签到 '{checkin['event_id']}' 计为已确认但缺少导师确认事件"
                )
        mentor_chain = [
            {
                "checkin_event_id": link.checkin_event_id,
                "confirmed_by_event_id": link.confirmed_by_event_id,
            }
            for link in links
        ]

    source = {
        "plan_version": snapshot.plan_version,
        "freeze_id": snapshot.freeze_id,
        "snapshot_generated_at": snapshot.generated_at,
        "event_cutoff_id": snapshot.event_cutoff_id,
    }
    source_checksum = checksum_of(source)

    body = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "attestation_id": attestation_id,
        "student_id": student_id,
        "purpose": purpose,
        "issued_at": _iso(issued_at),
        "valid_from": _iso(valid_from),
        "expires_at": _iso(expires_at) if expires_at is not None else None,
        "supersedes_id": supersedes_id,
        "source": source,
        "source_checksum": source_checksum,
        "hours": {
            "confirmed_seconds": student["confirmed_seconds"],
            "adjustment_seconds": student["adjustment_seconds"],
            "total_seconds": student["total_seconds"],
            "lesson_units": student["lesson_units"],
            "required_seconds": snapshot.required_seconds,
            "meets_requirement": bool(student["meets_requirement"]),
        },
        "mentor_confirmation_chain": mentor_chain,
    }
    body["checksum"] = checksum_of(_unsigned_body(body))
    return body


def _unsigned_body(package: dict[str, Any]) -> dict[str, Any]:
    """校验码覆盖除 checksum 外的全部字段。"""
    return {k: v for k, v in package.items() if k != "checksum"}


def verify_package(package: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """离线核验：不依赖数据库与网络。

    返回核验报告；任何关键项失败都会在 errors 中列出。
    """
    errors: list[str] = []
    instant = (now or utc_now()).astimezone(timezone.utc)

    if not isinstance(package, dict):
        raise AttestationError("数据包必须是 JSON 对象")

    claimed = package.get("checksum")
    actual = checksum_of(_unsigned_body(package))
    checksum_ok = isinstance(claimed, str) and claimed == actual
    if not checksum_ok:
        errors.append("校验码不匹配：数据包内容已被篡改或损坏")

    source = package.get("source")
    source_checksum_ok = False
    if isinstance(source, dict):
        source_checksum_ok = (
            package.get("source_checksum") == checksum_of(source)
        )
        if not source_checksum_ok:
            errors.append("来源摘要不匹配")
    else:
        errors.append("缺少冻结快照来源信息")

    expiry: datetime | None = None
    not_yet_valid = False
    expired = False
    try:
        if package.get("expires_at"):
            expiry = parse_iso(str(package["expires_at"]))
            expired = instant >= expiry
            if expired:
                errors.append("证明已超过有效期")
        valid_from = parse_iso(str(package.get("valid_from")))
        not_yet_valid = instant < valid_from
        if not_yet_valid:
            errors.append("证明尚未到生效时间")
    except (AttestationError, ValueError) as exc:
        errors.append(f"有效期字段无法解析：{exc}")

    chain = package.get("mentor_confirmation_chain", [])
    chain_ok = isinstance(chain, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("checkin_event_id"), str)
        and isinstance(item.get("confirmed_by_event_id"), str)
        for item in chain
    )
    if not chain_ok:
        errors.append("导师确认链格式无效")

    return {
        "valid": not errors,
        "attestation_id": package.get("attestation_id"),
        "checksum_ok": checksum_ok,
        "source_checksum_ok": source_checksum_ok,
        "mentor_chain_ok": chain_ok,
        "not_yet_valid": not_yet_valid,
        "expired": expired,
        "expires_at": expiry.isoformat().replace("+00:00", "Z") if expiry else None,
        "supersedes_id": package.get("supersedes_id"),
        "errors": errors,
        # 仅提示：离线无法获知在线撤销/替代状态。
        "revocation_check_required": True,
    }


def verify_snapshot_binding(
    package: dict[str, Any], snapshot: Snapshot
) -> bool:
    """核验数据包是否确实由给定的冻结快照生成。"""
    source = package.get("source")
    if not isinstance(source, dict):
        return False
    expected = {
        "plan_version": snapshot.plan_version,
        "freeze_id": snapshot.freeze_id,
        "snapshot_generated_at": snapshot.generated_at,
        "event_cutoff_id": snapshot.event_cutoff_id,
    }
    return source == expected and package.get("source_checksum") == checksum_of(expected)


def derive_totals_from_snapshot(
    snapshot: Snapshot, student_id: str
) -> dict[str, Any] | None:
    """供重启后比对：从存储的快照重新派生学生披露量。"""
    for student in snapshot.students:
        if student["student_id"] == student_id:
            return {
                "confirmed_seconds": student["confirmed_seconds"],
                "adjustment_seconds": student["adjustment_seconds"],
                "total_seconds": student["total_seconds"],
                "lesson_units": student["lesson_units"],
                "required_seconds": snapshot.required_seconds,
                "meets_requirement": bool(student["meets_requirement"]),
            }
    return None
