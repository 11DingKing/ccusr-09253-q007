"""学时证明申请、签发、撤销与核验的接口与领域测试。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import attestation_service as svc
from app.core import attestations as att
from app.models import Base
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _student_headers(actor: str) -> dict[str, str]:
    return {"X-Actor-Id": actor, "X-Actor-Role": "student"}


def _mentor_headers(actor: str) -> dict[str, str]:
    return {"X-Actor-Id": actor, "X-Actor-Role": "mentor"}


def _admin_headers(actor: str = "ADMIN") -> dict[str, str]:
    return {"X-Actor-Id": actor, "X-Actor-Role": "admin"}


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _mentor_confirm(eid, student, checkin_eid, mentor="M1"):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm",
        "student_id": student,
        "payload": {"checkin_event_id": checkin_eid, "mentor_id": mentor},
    }


def _leave(eid, student, seconds, reason="correction"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


@pytest.fixture
def frozen(client) -> str:
    """建方案、导入两个学生的事件并冻结 F-01，返回方案版本。"""
    pv = SHANGHAI_PLAN["plan_version"]
    client.post("/api/plans", json=SHANGHAI_PLAN)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                _mentor_confirm("E-02", "S1", "E-01", mentor="MENTOR-9"),
                _checkin(
                    "E-03",
                    "S2",
                    "2024-03-16T09:00:00+08:00",
                    "2024-03-16T11:30:00+08:00",
                ),
                _leave("E-04", "S1", -900, reason="late arrival make-up note"),
            ]
        },
    )
    resp = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert resp.status_code == 201, resp.text
    return pv


def _request(
    client,
    pv,
    cert_id,
    *,
    student="S1",
    purpose="graduation",
    actor="S1",
    role="student",
    applicant=None,
    ttl_days=30,
    supersedes_id=None,
    expires_at=None,
):
    headers = {
        "student": _student_headers,
        "mentor": _mentor_headers,
        "admin": _admin_headers,
    }[role](actor)
    body = {
        "student_id": student,
        "purpose": purpose,
        "applicant_id": applicant or actor,
    }
    if ttl_days is not None:
        body["ttl_days"] = ttl_days
    if expires_at is not None:
        body["expires_at"] = expires_at
    if supersedes_id is not None:
        body["supersedes_id"] = supersedes_id
    return client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/{cert_id}",
        json=body,
        headers=headers,
    )


def _approve(client, cert_id, approver="M1", reason="ok"):
    return client.post(
        f"/api/certificates/{cert_id}/approve",
        json={"approver_id": approver, "reason": reason},
        headers=_mentor_headers(approver),
    )


# ---------------------------------------------------------------------------
# 1. 权限范围
# ---------------------------------------------------------------------------


def test_request_requires_actor_header(client, frozen):
    pv = frozen
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-1",
        json={
            "student_id": "S1",
            "purpose": "graduation",
            "applicant_id": "S1",
            "ttl_days": 30,
        },
    )
    assert resp.status_code == 401


def test_student_may_only_request_for_self(client, frozen):
    pv = frozen
    # S1 试图为 S2 申请 -> 403
    resp = _request(client, pv, "C-X", student="S2", actor="S1")
    assert resp.status_code == 403
    # applicant_id 与操作人不一致 -> 403
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-Y",
        json={
            "student_id": "S1",
            "purpose": "job",
            "applicant_id": "SOMEONE_ELSE",
            "ttl_days": 30,
        },
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 403
    # 导师可以代为申请
    resp = _request(
        client, pv, "C-MENTOR", student="S1", actor="M1", role="mentor"
    )
    assert resp.status_code == 201, resp.text


def test_approval_and_revocation_role_scope(client, frozen):
    pv = frozen
    resp = _request(client, pv, "C-1")
    assert resp.status_code == 201
    # 学员不能审批
    bad = client.post(
        "/api/certificates/C-1/approve",
        json={"approver_id": "S1", "reason": "self"},
        headers=_student_headers("S1"),
    )
    assert bad.status_code == 403
    # approver_id 必须等于操作人
    spoof = client.post(
        "/api/certificates/C-1/approve",
        json={"approver_id": "M2", "reason": "x"},
        headers=_mentor_headers("M1"),
    )
    assert spoof.status_code == 403
    # 导师审批通过
    ok = _approve(client, "C-1")
    assert ok.status_code == 200, ok.text
    # 导师不能撤销，仅管理员可以
    denied = client.post(
        "/api/certificates/C-1/revoke",
        json={"approver_id": "M1", "reason": "try"},
        headers=_mentor_headers("M1"),
    )
    assert denied.status_code == 403
    revoked = client.post(
        "/api/certificates/C-1/revoke",
        json={"approver_id": "ADMIN", "reason": "fraud check"},
        headers=_admin_headers(),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"


def test_student_cannot_read_or_download_other_students_certificate(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    _approve(client, "C-1")
    other = client.get("/api/certificates/C-1", headers=_student_headers("S2"))
    assert other.status_code == 403
    dl = client.get("/api/certificates/C-1/download", headers=_student_headers("S2"))
    assert dl.status_code == 403
    # 导师可以查看与下载
    assert client.get("/api/certificates/C-1", headers=_mentor_headers("M1")).status_code == 200
    dl_ok = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    )
    assert dl_ok.status_code == 200
    assert dl_ok.headers["x-certificate-status"] == "issued"


# ---------------------------------------------------------------------------
# 2. 最小披露数据包与确认链
# ---------------------------------------------------------------------------


def test_issued_package_is_minimal_disclosure_with_confirmation_chain(client, frozen):
    pv = frozen
    _request(client, pv, "C-1", ttl_days=30)
    _approve(client, "C-1")
    package = client.get(
        "/api/certificates/C-1/download", headers=_student_headers("S1")
    ).json()

    assert package["schema_version"] == "attestation/v1"
    assert package["certificate_id"] == "C-1"
    assert package["student_id"] == "S1"
    assert package["plan_version"] == pv
    assert package["purpose"] == "graduation"
    assert package["freeze"]["freeze_id"] == "F-01"
    assert package["freeze"]["event_cutoff_id"] == "E-04"
    assert package["supersedes"] is None
    assert package["expires_at"] is not None
    assert package["checksum"].startswith("sha256:")

    claims = package["claims"]
    # 汇总学时：2 小时签到 - 15 分钟修正 = 6300 秒
    assert claims["confirmed_seconds"] == 7200
    assert claims["adjustment_seconds"] == -900
    assert claims["total_seconds"] == 6300
    assert claims["lesson_units"] == 2
    assert claims["meets_requirement"] is False

    # 确认链：E-01 由导师确认事件 E-02 / MENTOR-9 背书
    chain = claims["confirmation_chain"]
    assert len(chain) == 1
    link = chain[0]
    assert link["checkin_event_id"] == "E-01"
    assert link["status"] == "CONFIRMED"
    assert link["confirmed_by_event_id"] == "E-02"
    assert link["mentor_id"] == "MENTOR-9"
    # 确认链不披露签到时间区间
    assert "check_in_at_utc" not in link
    # 修正只披露事件编号与数值，不披露原因文本
    assert claims["adjustments"] == [{"event_id": "E-04", "seconds": -900}]

    # 不包含每日明细、完整签到解释或其他学生数据
    assert "daily" not in claims
    assert "checkins" not in claims
    serialized = json.dumps(package)
    assert "late arrival make-up note" not in serialized
    assert "S2" not in serialized
    assert "10:00" not in serialized and "08:00" not in serialized


def test_package_checksum_covers_full_envelope(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    _approve(client, "C-1")
    package = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    ).json()
    # 服务端重算校验码一致
    assert att.compute_checksum(package) == package["checksum"]
    # 任何字段被篡改都会导致校验码失配
    tampered = dict(package)
    tampered["purpose"] = "tampered"
    result = att.verify_package(
        tampered, now=datetime.now(timezone.utc), registry_status="issued"
    )
    assert result["valid"] is False
    assert result["checks"]["checksum"] is False


# ---------------------------------------------------------------------------
# 3. 幂等
# ---------------------------------------------------------------------------


def test_repeated_request_is_idempotent(client, frozen):
    pv = frozen
    first = _request(client, pv, "C-1")
    assert first.status_code == 201, first.text
    second = _request(client, pv, "C-1")
    assert second.status_code == 200
    assert second.json()["certificate_id"] == "C-1"
    assert second.json()["status"] == "pending"

    # 审批后再次申请同编号同参数：仍然是同一份已签发证明，数据包不变
    _approve(client, "C-1")
    third = _request(client, pv, "C-1")
    assert third.status_code == 200
    assert third.json()["status"] == "issued"
    pkg1 = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    ).json()
    _approve(client, "C-1")  # 重复审批幂等
    pkg2 = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    ).json()
    assert pkg1 == pkg2
    assert pkg1["issued_at"] == pkg2["issued_at"]


def test_same_idempotency_key_different_certificate_id_conflicts(client, frozen):
    pv = frozen
    assert _request(client, pv, "C-1").status_code == 201
    clash = _request(client, pv, "C-2")
    assert clash.status_code == 409
    # 不同用途是不同申请，允许共存
    other = _request(client, pv, "C-3", purpose="employment")
    assert other.status_code == 201


def test_reject_idempotent_and_blocks_approval(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    first = client.post(
        "/api/certificates/C-1/reject",
        json={"approver_id": "M1", "reason": "incomplete"},
        headers=_mentor_headers("M1"),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"
    # 重复驳回幂等
    second = client.post(
        "/api/certificates/C-1/reject",
        json={"approver_id": "M1", "reason": "incomplete again"},
        headers=_mentor_headers("M1"),
    )
    assert second.status_code == 200
    # 驳回后不能再批准
    assert _approve(client, "C-1").status_code == 409
    # 驳回的证明没有可下载的数据包
    dl = client.get("/api/certificates/C-1/download", headers=_mentor_headers("M1"))
    assert dl.status_code == 409
    # 驳回必须填写原因
    _request(client, pv, "C-2")
    bad = client.post(
        "/api/certificates/C-2/reject",
        json={"approver_id": "M1", "reason": ""},
        headers=_mentor_headers("M1"),
    )
    assert bad.status_code == 422


def test_revoke_is_idempotent_and_requires_issued(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    # pending 不能撤销
    denied = client.post(
        "/api/certificates/C-1/revoke",
        json={"approver_id": "ADMIN", "reason": "x"},
        headers=_admin_headers(),
    )
    assert denied.status_code == 409
    _approve(client, "C-1")
    payload = {"approver_id": "ADMIN", "reason": "issued by mistake"}
    first = client.post(
        "/api/certificates/C-1/revoke", json=payload, headers=_admin_headers()
    )
    assert first.status_code == 200
    assert first.json()["revoked_reason"] == "issued by mistake"
    second = client.post(
        "/api/certificates/C-1/revoke", json=payload, headers=_admin_headers()
    )
    assert second.status_code == 200
    assert second.json()["status"] == "revoked"


# ---------------------------------------------------------------------------
# 4. 并发签发
# ---------------------------------------------------------------------------


def test_concurrent_approval_only_one_issuance(client, frozen):
    pv = frozen
    _request(client, pv, "C-CONC")
    created_flags: list[bool] = []
    lock = threading.Lock()

    def _approve():
        session = TestSessionLocal()
        try:
            _, created = svc.approve_certificate(
                session,
                certificate_id="C-CONC",
                approver_id="M1",
                reason="concurrent",
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_approve) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 4

    detail = client.get(
        "/api/certificates/C-CONC", headers=_mentor_headers("M1")
    ).json()
    assert detail["status"] == "issued"
    approve_entries = [a for a in detail["audit"] if a["action"] == "approve"]
    assert len(approve_entries) == 1


def test_concurrent_requests_same_id_only_one_row(client, frozen):
    pv = frozen
    created_flags: list[bool] = []
    lock = threading.Lock()

    def _request():
        session = TestSessionLocal()
        try:
            _, created = svc.request_certificate(
                session,
                certificate_id="C-RACER",
                plan_version=pv,
                freeze_id="F-01",
                student_id="S1",
                purpose="graduation",
                applicant_id="S1",
                ttl_days=30,
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_request) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 4


# ---------------------------------------------------------------------------
# 5. 过期
# ---------------------------------------------------------------------------


def test_expired_certificate_fails_verification(client, frozen):
    pv = frozen
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    resp = _request(client, pv, "C-EXP", ttl_days=None, expires_at=past)
    assert resp.status_code == 201, resp.text
    _approve(client, "C-EXP")

    detail = client.get("/api/certificates/C-EXP", headers=_mentor_headers("M1")).json()
    assert detail["expired"] is True

    result = client.post(
        "/api/certificates/verify", json={"certificate_id": "C-EXP"}
    ).json()
    assert result["valid"] is False
    assert result["checks"]["not_expired"] is False
    assert any("expired" in r for r in result["reasons"])


def test_verify_core_respects_now():
    future_issue = datetime(2026, 1, 1, tzinfo=timezone.utc)
    package = att.build_package(
        certificate_id="C-T",
        student_id="S1",
        plan_version="P",
        purpose="x",
        freeze_id="F",
        freeze_generated_at="2025-12-01T00:00:00Z",
        event_cutoff_id="E-1",
        claims={"total_seconds": 1},
        source_fingerprint="sha256:abc",
        issued_at=future_issue,
        expires_at=future_issue + timedelta(days=10),
        supersedes_id=None,
    )
    before = att.verify_package(package, now=future_issue + timedelta(days=1))
    after = att.verify_package(package, now=future_issue + timedelta(days=11))
    assert before["valid"] is True
    assert after["valid"] is False
    assert after["checks"]["not_expired"] is False


# ---------------------------------------------------------------------------
# 6. 冻结不变性 + 纠错替代链
# ---------------------------------------------------------------------------


def test_late_correction_requires_replacement_certificate(client, frozen):
    pv = frozen
    _request(client, pv, "C-OLD")
    _approve(client, "C-OLD")
    old_package = client.get(
        "/api/certificates/C-OLD/download", headers=_mentor_headers("M1")
    ).json()
    assert old_package["claims"]["total_seconds"] == 6300

    # 冻结之后到达新的修正事件（+1 小时），旧冻结快照保持不变
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_leave("E-09", "S1", 3600, reason="approved make-up")]},
    )
    client.post(f"/api/plans/{pv}/freezes/F-02", json={})
    old_freeze = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert old_freeze["event_cutoff_id"] == "E-04"
    assert old_freeze["students"][0]["total_seconds"] == 6300

    # 不能用旧冻结上的同参数重复申请；纠错必须走替代证明（新用途/新编号）
    replacement_body = {
        "student_id": "S1",
        "purpose": "graduation-corrected",
        "applicant_id": "S1",
        "ttl_days": 30,
        "supersedes_id": "C-OLD",
    }
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-02/certificates/C-NEW",
        json=replacement_body,
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 201, resp.text
    _approve(client, "C-NEW")

    new_package = client.get(
        "/api/certificates/C-NEW/download", headers=_mentor_headers("M1")
    ).json()
    assert new_package["supersedes"] == "C-OLD"
    assert new_package["freeze"]["freeze_id"] == "F-02"
    assert new_package["claims"]["total_seconds"] == 6300 + 3600

    # 旧证明被标记 superseded；其原始数据包仍可下载但核验不通过
    old_detail = client.get(
        "/api/certificates/C-OLD", headers=_mentor_headers("M1")
    ).json()
    assert old_detail["status"] == "superseded"
    old_verify = client.post(
        "/api/certificates/verify", json={"certificate_id": "C-OLD"}
    ).json()
    assert old_verify["valid"] is False
    assert any("superseded" in r for r in old_verify["reasons"])

    new_verify = client.post(
        "/api/certificates/verify", json={"certificate_id": "C-NEW"}
    ).json()
    assert new_verify["valid"] is True
    assert new_verify["checks"]["source_fingerprint"] == "match"

    # 旧证明的数据包自签发以来从未被修改
    old_package_after = client.get(
        "/api/certificates/C-OLD/download", headers=_mentor_headers("M1")
    ).json()
    assert old_package_after == old_package


def test_replacement_must_target_same_student_and_existing_cert(client, frozen):
    pv = frozen
    # 链接不存在的旧编号 -> 404
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-R1",
        json={
            "student_id": "S1",
            "purpose": "fix-1",
            "applicant_id": "S1",
            "ttl_days": 30,
            "supersedes_id": "C-GHOST",
        },
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 404

    _request(client, pv, "C-OLD", purpose="orig")
    _approve(client, "C-OLD")
    # 替代证明指向不同学生 -> 422
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-R2",
        json={
            "student_id": "S2",
            "purpose": "fix-2",
            "applicant_id": "S2",
            "ttl_days": 30,
            "supersedes_id": "C-OLD",
        },
        headers=_student_headers("S2"),
    )
    assert resp.status_code == 422


def test_pending_certificate_cannot_be_superseded(client, frozen):
    pv = frozen
    _request(client, pv, "C-PENDING", purpose="orig")
    # 旧证明尚未审批签发时不能被替代 -> 409
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-FIX",
        json={
            "student_id": "S1",
            "purpose": "fix-after-issue",
            "applicant_id": "S1",
            "ttl_days": 30,
            "supersedes_id": "C-PENDING",
        },
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 409


def test_replacement_can_follow_revoked_certificate_without_resurrecting_it(client, frozen):
    pv = frozen
    _request(client, pv, "C-OLD", purpose="graduation")
    _approve(client, "C-OLD")
    client.post(
        "/api/certificates/C-OLD/revoke",
        json={"approver_id": "ADMIN", "reason": "needs correction"},
        headers=_admin_headers(),
    )
    # 撤销后仍可基于同一冻结申请替代证明并链接旧编号（如用途不变也允许）
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-FIX",
        json={
            "student_id": "S1",
            "purpose": "graduation-v2",
            "applicant_id": "S1",
            "ttl_days": 30,
            "supersedes_id": "C-OLD",
        },
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 201, resp.text
    _approve(client, "C-FIX")

    new_package = client.get(
        "/api/certificates/C-FIX/download", headers=_mentor_headers("M1")
    ).json()
    assert new_package["supersedes"] == "C-OLD"
    # 旧证明保持 revoked 终态，不被改标
    old_detail = client.get(
        "/api/certificates/C-OLD", headers=_admin_headers()
    ).json()
    assert old_detail["status"] == "revoked"
    assert old_detail["revoked_reason"] == "needs correction"
    # 新证明核验通过
    new_verify = client.post(
        "/api/certificates/verify", json={"certificate_id": "C-FIX"}
    ).json()
    assert new_verify["valid"] is True


# ---------------------------------------------------------------------------
# 7. 撤销核验与冻结来源指纹
# ---------------------------------------------------------------------------


def test_revoked_certificate_fails_online_verification(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    _approve(client, "C-1")
    client.post(
        "/api/certificates/C-1/revoke",
        json={"approver_id": "ADMIN", "reason": "data dispute"},
        headers=_admin_headers(),
    )
    result = client.post(
        "/api/certificates/verify", json={"certificate_id": "C-1"}
    ).json()
    assert result["valid"] is False
    assert result["registry_status"] == "revoked"
    assert any("revoked" in r for r in result["reasons"])


def test_unknown_certificate_and_missing_freeze(client, frozen):
    pv = frozen
    assert (
        client.post(
            "/api/certificates/verify", json={"certificate_id": "C-NOPE"}
        ).status_code
        == 404
    )
    # 冻结不存在 -> 404；学生不在冻结中 -> 404
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-99/certificates/C-X",
        json={
            "student_id": "S1",
            "purpose": "job",
            "applicant_id": "S1",
            "ttl_days": 10,
        },
        headers=_student_headers("S1"),
    )
    assert resp.status_code == 404
    resp = client.post(
        f"/api/plans/{pv}/freezes/F-01/certificates/C-Y",
        json={
            "student_id": "S404",
            "purpose": "job",
            "applicant_id": "S404",
            "ttl_days": 10,
        },
        headers=_student_headers("S404"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 8. 离线核验
# ---------------------------------------------------------------------------


def test_offline_verification_with_unknown_registry(client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    _approve(client, "C-1")
    package = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    ).json()
    # 上传数据包，服务端本地无此编号登记（模拟外部核验方）：仍做结构/校验码/有效期核验。
    # 改编号后重算校验码，以模拟另一签发方真实签发的外来证明。
    foreign = {**package, "certificate_id": "C-UNKNOWN-ID"}
    foreign["checksum"] = att.compute_checksum(foreign)
    result = client.post(
        "/api/certificates/verify",
        json={"package": foreign},
    ).json()
    assert result["valid"] is True
    assert result["checks"]["checksum"] is True
    assert result["checks"]["structure"] is True
    assert result["checks"]["registry"] == "unknown"
    # 无本地登记时不附带登记状态
    assert result.get("registry_status") is None

    # 篡改后的数据包离线核验必失败
    tampered = {**package, "certificate_id": "C-UNKNOWN-ID"}
    tampered["claims"] = {**tampered["claims"], "total_seconds": 999999}
    bad = client.post("/api/certificates/verify", json={"package": tampered}).json()
    assert bad["valid"] is False
    assert bad["checks"]["checksum"] is False


def test_offline_verification_needs_package_or_id(client):
    resp = client.post("/api/certificates/verify", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 9. 重启后校验
# ---------------------------------------------------------------------------


def test_certificate_survives_restart_and_offline_verifier(tmp_path, client, frozen):
    pv = frozen
    _request(client, pv, "C-1")
    _approve(client, "C-1")
    package = client.get(
        "/api/certificates/C-1/download", headers=_mentor_headers("M1")
    ).json()

    # 用全新的数据库文件模拟“核验方”：无任何登记记录
    verifier_path = tmp_path / "verifier.db"
    verifier_engine = create_engine(f"sqlite:///{verifier_path}", future=True)
    Base.metadata.create_all(verifier_engine)
    VerifierSession = sessionmaker(bind=verifier_engine, future=True)
    vsession = VerifierSession()
    try:
        result = svc.verify_certificate(vsession, package=dict(package))
    finally:
        vsession.close()
        verifier_engine.dispose()
    assert result["valid"] is True
    assert result["checks"]["source_fingerprint"] == "unavailable"
    assert result["checks"]["registry"] == "unknown"

    # 纯函数离线核验（无数据库、无网络）
    pure = att.verify_package(json.loads(json.dumps(package)), now=datetime.now(timezone.utc))
    assert pure["valid"] is True

    # 模拟签发服务重启：新建引擎指向同一个测试库文件，登记状态与数据包仍可校验
    _ = frozen  # client fixture 使用的文件
    restarted_engine = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False},
        future=True,
    )
    RestartedSession = sessionmaker(bind=restarted_engine, future=True)
    rsession = RestartedSession()
    try:
        online = svc.verify_certificate(rsession, certificate_id="C-1")
        again = svc.verify_certificate(rsession, package=dict(package))
    finally:
        rsession.close()
        restarted_engine.dispose()
    assert online["valid"] is True
    assert online["registry_status"] == "issued"
    assert online["checks"]["source_fingerprint"] == "match"
    assert again["valid"] is True
    assert again["registry_status"] == "issued"


# ---------------------------------------------------------------------------
# 10. 审计链
# ---------------------------------------------------------------------------


def test_certificate_audit_trail_records_lifecycle(client, frozen):
    pv = frozen
    _request(client, pv, "C-1", purpose="job")
    _approve(client, "C-1", approver="M1", reason="all good")
    client.post(
        "/api/certificates/C-1/revoke",
        json={"approver_id": "ADMIN", "reason": "later dispute"},
        headers=_admin_headers(),
    )
    detail = client.get(
        "/api/certificates/C-1", headers=_admin_headers()
    ).json()
    actions = [(a["sequence"], a["action"], a["actor_id"]) for a in detail["audit"]]
    assert actions == [
        (1, "request", "S1"),
        (2, "approve", "M1"),
        (3, "revoke", "ADMIN"),
    ]
    assert detail["audit"][1]["detail"] == "all good"
