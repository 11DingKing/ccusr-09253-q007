"""学时证明数据包的构建与离线核验（纯领域逻辑，不依赖数据库）。

证明数据包从**冻结快照**中的单个学生条目派生，只披露核验所需的最小字段：
汇总学时、学时单元、达标结论、签到确认链与修正事件编号；不导出每日明细、
其他学生数据或修正原因文本。

数据包自带两级锚点：

* ``checksum``：对除校验码外的整个封包做规范化 SHA-256，任何篡改立即可验；
* ``source_fingerprint``：对来源冻结快照（含该学生完整条目）的哈希，在线核验
  时可由服务端重算比对，证明数据包确实派生自指定冻结版本。

核验函数不要求调用方持有数据库连接：``registry_status`` 与
``source_fingerprint`` 均为可选入参，缺省时即为纯离线核验。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from .replay import Event, EventType

SCHEMA_VERSION = "attestation/v1"

_REQUIRED_TOP_KEYS = (
    "schema_version",
    "certificate_id",
    "student_id",
    "plan_version",
    "purpose",
    "freeze",
    "issued_at",
    "expires_at",
    "supersedes",
    "claims",
    "source_fingerprint",
    "checksum",
)
_REQUIRED_FREEZE_KEYS = ("freeze_id", "generated_at", "event_cutoff_id")


def utc_iso(value: datetime) -> str:
    """执行确定性的业务处理。"""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """执行确定性的业务处理。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json(obj: Any) -> bytes:
    """规范化序列化：键排序、无空白、UTF-8，保证跨进程可重算。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(payload: bytes) -> str:
    """执行确定性的业务处理。"""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def compute_checksum(package: dict[str, Any]) -> str:
    """对除 ``checksum`` 外的封包内容计算校验码。"""
    envelope = {k: v for k, v in package.items() if k != "checksum"}
    return digest(canonical_json(envelope))


def build_confirmation_index(
    events: list[Event], student_id: str
) -> dict[str, tuple[str, str | None]]:
    """从截止事件中提取 签到事件编号 -> (导师确认事件编号, 导师标识) 映射。

    与重放规则一致：确认事件必须属于同一学生才生效；导师标识取自确认事件
    payload 中可选的 ``mentor_id``。
    """
    index: dict[str, tuple[str, str | None]] = {}
    for event in events:
        if event.event_type != EventType.MENTOR_CONFIRM:
            continue
        if event.student_id != student_id:
            continue
        target = event.payload.get("checkin_event_id")
        if isinstance(target, str) and target not in index:
            mentor_id = event.payload.get("mentor_id")
            index[target] = (event.event_id, str(mentor_id) if mentor_id else None)
    return index


def build_confirmation_chain(
    student_snapshot: dict[str, Any], events: list[Event]
) -> list[dict[str, Any]]:
    """构造最小披露的导师确认链（不含签到时间区间等额外信息）。"""
    student_id = student_snapshot["student_id"]
    confirms = build_confirmation_index(events, student_id)
    chain: list[dict[str, Any]] = []
    for item in student_snapshot.get("checkins", []):
        confirmation = confirms.get(item["event_id"])
        chain.append(
            {
                "checkin_event_id": item["event_id"],
                "activity_type": item.get("activity_type", "regular"),
                "status": item["status"],
                "confirmed_by_event_id": confirmation[0] if confirmation else None,
                "mentor_id": confirmation[1] if confirmation else None,
            }
        )
    return chain


def build_claims(
    *,
    timezone_name: str,
    required_seconds: int,
    student: dict[str, Any],
    confirmation_chain: list[dict[str, Any]],
) -> dict[str, Any]:
    """从冻结快照的学生条目抽取最小披露声明。"""
    return {
        "timezone": timezone_name,
        "required_seconds": required_seconds,
        "confirmed_seconds": student["confirmed_seconds"],
        "adjustment_seconds": student["adjustment_seconds"],
        "total_seconds": student["total_seconds"],
        "lesson_units": student["lesson_units"],
        "meets_requirement": student["meets_requirement"],
        "confirmation_chain": confirmation_chain,
        "adjustments": [
            {"event_id": item["event_id"], "seconds": item["seconds"]}
            for item in student.get("adjustments", [])
        ],
    }


def build_source_fingerprint(
    *,
    plan_version: str,
    freeze_id: str,
    freeze_generated_at: str,
    event_cutoff_id: str | None,
    student: dict[str, Any],
) -> str:
    """对来源冻结快照中的学生完整条目取指纹，锚定冻结版本。"""
    body = {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "freeze_generated_at": freeze_generated_at,
        "event_cutoff_id": event_cutoff_id,
        "student": student,
    }
    return digest(canonical_json(body))


def build_package(
    *,
    certificate_id: str,
    student_id: str,
    plan_version: str,
    purpose: str,
    freeze_id: str,
    freeze_generated_at: str,
    event_cutoff_id: str | None,
    claims: dict[str, Any],
    source_fingerprint: str,
    issued_at: datetime,
    expires_at: datetime | None,
    supersedes_id: str | None,
) -> dict[str, Any]:
    """组装证明数据包并填充校验码。"""
    package: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "certificate_id": certificate_id,
        "student_id": student_id,
        "plan_version": plan_version,
        "purpose": purpose,
        "freeze": {
            "freeze_id": freeze_id,
            "generated_at": freeze_generated_at,
            "event_cutoff_id": event_cutoff_id,
        },
        "issued_at": utc_iso(issued_at),
        "expires_at": utc_iso(expires_at) if expires_at is not None else None,
        "supersedes": supersedes_id,
        "claims": claims,
        "source_fingerprint": source_fingerprint,
    }
    package["checksum"] = compute_checksum(package)
    return package


def verify_package(
    package: Any,
    *,
    now: datetime,
    registry_status: str | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    """核验证明数据包。

    ``registry_status`` 为服务端登记状态（未知编号传 ``None``）；
    ``source_fingerprint`` 为在线重算的来源指纹（无冻结数据时传 ``None``）。
    两者缺省时即为纯离线核验：只校验结构、校验码与有效期。
    """
    instant = now.astimezone(timezone.utc)
    checks: dict[str, Any] = {
        "structure": False,
        "checksum": False,
        "not_expired": False,
        "source_fingerprint": "unavailable",
        "registry": registry_status or "unknown",
    }
    reasons: list[str] = []
    certificate_id = package.get("certificate_id") if isinstance(package, dict) else None

    if not isinstance(package, dict):
        reasons.append("package must be a JSON object")
        return {
            "certificate_id": None,
            "valid": False,
            "checks": checks,
            "reasons": reasons,
        }

    freeze_block = package.get("freeze")
    structure_ok = True
    if package.get("schema_version") != SCHEMA_VERSION:
        structure_ok = False
        reasons.append(f"unsupported schema_version (expected {SCHEMA_VERSION})")
    missing = [key for key in _REQUIRED_TOP_KEYS if key not in package]
    if missing:
        structure_ok = False
        reasons.append(f"missing fields: {', '.join(sorted(missing))}")
    if not isinstance(freeze_block, dict) or any(
        key not in freeze_block for key in _REQUIRED_FREEZE_KEYS
    ):
        structure_ok = False
        reasons.append("freeze block is incomplete")
    if not isinstance(package.get("claims"), dict):
        structure_ok = False
        reasons.append("claims must be an object")
    checks["structure"] = structure_ok

    if structure_ok:
        expected_checksum = compute_checksum(package)
        checksum_ok = hmac.compare_digest(
            expected_checksum, str(package.get("checksum"))
        )
        checks["checksum"] = checksum_ok
        if not checksum_ok:
            reasons.append("checksum mismatch: package content was altered")

        raw_expires = package.get("expires_at")
        if raw_expires is None:
            checks["not_expired"] = True
        else:
            try:
                checks["not_expired"] = parse_iso(str(raw_expires)) > instant
            except ValueError:
                reasons.append("expires_at is not a valid ISO-8601 timestamp")
        if not checks["not_expired"]:
            reasons.append("certificate has expired")

        if source_fingerprint is not None:
            source_ok = hmac.compare_digest(
                source_fingerprint, str(package.get("source_fingerprint"))
            )
            checks["source_fingerprint"] = "match" if source_ok else "mismatch"
            if not source_ok:
                reasons.append("source fingerprint does not match the frozen snapshot")

    status = registry_status or "unknown"
    if status == "revoked":
        reasons.append("certificate has been revoked")
    elif status == "superseded":
        reasons.append("certificate has been superseded by a newer replacement")
    elif status in ("pending", "rejected"):
        reasons.append(f"certificate is not issued (status={status})")

    valid = bool(
        checks["structure"]
        and checks["checksum"]
        and checks["not_expired"]
        and checks["source_fingerprint"] != "mismatch"
        and status in ("issued", "unknown")
    )
    return {
        "certificate_id": certificate_id,
        "valid": valid,
        "checks": checks,
        "reasons": reasons,
    }
