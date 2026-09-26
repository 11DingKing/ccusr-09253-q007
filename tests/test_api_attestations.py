"""学时证明申请/签发/撤销/核验接口测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import attestation_services
from app.core import attestation as domain
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

STUDENT_HEADERS = {"X-Actor-Id": "S1", "X-Actor-Role": "student"}
OTHER_HEADERS = {"X-Actor-Id": "S2", "X-Actor-Role": "student"}
MENTOR_HEADERS = {"X-Actor-Id": "M1", "X-Actor-Role": "mentor"}
ADMIN_HEADERS = {"X-Actor-Id": "A1", "X-Actor-Role": "admin"}


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


@pytest.fixture
def frozen_plan(client):
    """培养方案 + 实习签到（导师已确认）+ 普通签到，冻结为 F-01。"""
    client.post("/api/plans", json=SHANGHAI_PLAN)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                    activity_type="internship",
                ),
                {
                    "event_id": "E-02",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-01"},
                },
                _checkin(
                    "E-03",
                    "S2",
                    "2024-03-15T09:00:00+08:00",
                    "2024-03-15T11:00:00+08:00",
                ),
            ]
        },
    )
    resp = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert resp.status_code == 201
    return pv


def _apply(client, pv, request_id="REQ-1", headers=STUDENT_HEADERS, student="S1"):
    return client.post(
        f"/api/plans/{pv}/attestations",
        json={
            "freeze_id": "F-01",
            "student_id": student,
            "purpose": "graduate admission",
            "request_id": request_id,
        },
        headers=headers,
    )


def _issue(client, attestation_id, headers=MENTOR_HEADERS, **extra):
    body = {"valid_for_days": 30}
    body.update(extra)
    return client.post(
        f"/api/attestations/{attestation_id}/approve", json=body, headers=headers
    )


# ---------------------------------------------------------------------------
# 申请与最小披露
# ---------------------------------------------------------------------------


def test_student_applies_and_downloads_minimal_package(client, frozen_plan):
    resp = _apply(client, frozen_plan)
    assert resp.status_code == 201, resp.text
    att = resp.json()
    assert att["status"] == "pending"
    assert att["attestation_id"].startswith("ATT-")
    assert att["student_id"] == "S1"
    assert att["checksum"] is None

    issued = _issue(client, att["attestation_id"])
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["status"] == "issued"
    assert body["decided_by"] == "M1"
    assert body["checksum"] and len(body["checksum"]) == 64

    pkg = client.get(
        f"/api/attestations/{body['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    )
    assert pkg.status_code == 200
    package = pkg.json()

    # 最小披露：只有本人、只有汇总量，无原始签到/其他学生/快照全文。
    assert package["student_id"] == "S1"
    assert package["purpose"] == "graduate admission"
    assert package["hours"]["total_seconds"] == 7200
    assert package["hours"]["lesson_units"] == 2
    assert package["hours"]["meets_requirement"] is False
    assert "students" not in package
    assert "S2" not in str(package)
    assert "check_in_at" not in str(package)
    # 导师确认链随包提供。
    assert package["mentor_confirmation_chain"] == [
        {"checkin_event_id": "E-01", "confirmed_by_event_id": "E-02"}
    ]
    assert package["source"] == {
        "plan_version": frozen_plan,
        "freeze_id": "F-01",
        "event_cutoff_id": "E-03",
        "snapshot_generated_at": package["source"]["snapshot_generated_at"],
    }


def test_apply_requires_student_in_freeze(client, frozen_plan):
    # 先建一个只存在于实时数据、但不在冻结点之前的学生。
    resp = client.post(
        f"/api/plans/{frozen_plan}/attestations",
        json={
            "freeze_id": "F-01",
            "student_id": "GHOST",
            "purpose": "job",
            "request_id": "REQ-GHOST",
        },
        headers={"X-Actor-Id": "GHOST", "X-Actor-Role": "student"},
    )
    assert resp.status_code == 400


def test_apply_against_unknown_freeze_404(client, frozen_plan):
    resp = client.post(
        f"/api/plans/{frozen_plan}/attestations",
        json={
            "freeze_id": "F-NOPE",
            "student_id": "S1",
            "purpose": "job",
            "request_id": "REQ-X",
        },
        headers=STUDENT_HEADERS,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 权限范围
# ---------------------------------------------------------------------------


def test_permission_scope_enforced(client, frozen_plan):
    att = _apply(client, frozen_plan).json()
    aid = att["attestation_id"]

    # 其他学生不能查看/列举/下载。
    assert client.get(f"/api/attestations/{aid}", headers=OTHER_HEADERS).status_code == 403
    assert (
        client.get(
            f"/api/plans/{frozen_plan}/students/S1/attestations",
            headers=OTHER_HEADERS,
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/attestations/{aid}/package", headers=OTHER_HEADERS
        ).status_code
        == 403
    )

    # 学生不能审批/驳回/撤销。
    assert _issue(client, aid, headers=STUDENT_HEADERS).status_code == 403
    assert (
        client.post(
            f"/api/attestations/{aid}/reject",
            json={"note": "no"},
            headers=STUDENT_HEADERS,
        ).status_code
        == 403
    )

    # 学生不能为他人申请。
    assert _apply(client, frozen_plan, request_id="REQ-2", headers=OTHER_HEADERS, student="S1").status_code == 403

    # 无身份头按匿名学生处理，同样被拒。
    assert client.get(f"/api/attestations/{aid}").status_code == 403
    assert client.get(f"/api/attestations/{aid}/self-check", headers=MENTOR_HEADERS).status_code in (200, 404)
    assert client.get(f"/api/attestations/{aid}/self-check").status_code == 403

    # 导师可审批；管理员可查看任意学生。
    assert _issue(client, aid, headers=MENTOR_HEADERS).status_code == 201
    listing = client.get(
        f"/api/plans/{frozen_plan}/students/S1/attestations", headers=ADMIN_HEADERS
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_reject_blocks_download_and_is_terminal(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-R").json()
    resp = client.post(
        f"/api/attestations/{att['attestation_id']}/reject",
        json={"note": "incomplete records"},
        headers=MENTOR_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    pkg = client.get(
        f"/api/attestations/{att['attestation_id']}/package", headers=STUDENT_HEADERS
    )
    assert pkg.status_code == 409

    # 再次驳回/签发都不能改变终态。
    again = client.post(
        f"/api/attestations/{att['attestation_id']}/reject",
        json={"note": "again"},
        headers=MENTOR_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["status"] == "rejected"
    assert _issue(client, att["attestation_id"]).status_code == 200
    assert _issue(client, att["attestation_id"]).json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_duplicate_apply_is_idempotent(client, frozen_plan):
    first = _apply(client, frozen_plan, request_id="REQ-IDEM")
    assert first.status_code == 201
    second = _apply(client, frozen_plan, request_id="REQ-IDEM")
    assert second.status_code == 200  # 幂等重放不返回 201
    assert second.json()["attestation_id"] == first.json()["attestation_id"]

    listing = client.get(
        f"/api/plans/{frozen_plan}/students/S1/attestations", headers=STUDENT_HEADERS
    ).json()
    assert len(listing) == 1


def test_idempotency_key_cannot_be_reused_by_other_actor(client, frozen_plan):
    assert _apply(client, frozen_plan, request_id="REQ-SHARED").status_code == 201
    # S2 试图用同一 request_id 给自己申请。
    resp = _apply(
        client,
        frozen_plan,
        request_id="REQ-SHARED",
        headers={"X-Actor-Id": "S2", "X-Actor-Role": "student"},
        student="S2",
    )
    assert resp.status_code == 403


def test_duplicate_approve_keeps_same_package(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-DUP").json()
    i1 = _issue(client, att["attestation_id"]).json()
    i2 = _issue(client, att["attestation_id"]).json()
    assert i1["checksum"] == i2["checksum"]
    assert i1["issued_at"] == i2["issued_at"]


# ---------------------------------------------------------------------------
# 并发签发
# ---------------------------------------------------------------------------


def test_concurrent_approval_only_one_issues(db, client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-CONC").json()
    aid = att["attestation_id"]

    outcomes: list[bool] = []
    lock = threading.Lock()

    def _approve():
        session = TestSessionLocal()
        try:
            from app.actors import Actor

            _, issued = attestation_services.approve(
                session,
                Actor(actor_id="M1", role="mentor"),
                aid,
                valid_for_days=30,
            )
            with lock:
                outcomes.append(issued)
        finally:
            session.close()

    threads = [threading.Thread(target=_approve) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(outcomes) == 1
    stored = client.get(f"/api/attestations/{aid}", headers=MENTOR_HEADERS).json()
    assert stored["status"] == "issued"
    pkg = client.get(
        f"/api/attestations/{aid}/package", headers=STUDENT_HEADERS
    ).json()
    report = domain.verify_package(pkg)
    assert report["valid"] is True


# ---------------------------------------------------------------------------
# 有效期 / 过期 / 未生效
# ---------------------------------------------------------------------------


def test_expired_package_fails_verification(db, client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-EXP").json()
    issued = _issue(client, att["attestation_id"], valid_for_days=10).json()
    pkg = client.get(
        f"/api/attestations/{issued['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    ).json()

    now = datetime.now(timezone.utc)
    assert domain.verify_package(pkg, now=now)["expired"] is False

    future = now + timedelta(days=11)
    report = domain.verify_package(pkg, now=future)
    assert report["expired"] is True
    assert report["valid"] is False
    assert any("有效期" in e for e in report["errors"])


def test_not_yet_valid_package_rejected(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-FUT").json()
    future_start = datetime.now(timezone.utc) + timedelta(hours=2)
    issued = _issue(
        client,
        att["attestation_id"],
        valid_from=future_start.isoformat(),
        valid_for_days=30,
    )
    assert issued.status_code == 201, issued.text
    pkg = client.get(
        f"/api/attestations/{issued.json()['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    ).json()
    report = domain.verify_package(pkg)
    assert report["not_yet_valid"] is True
    assert report["valid"] is False


def test_explicit_expires_at_must_be_future(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-PAST").json()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    resp = _issue(client, att["attestation_id"], expires_at=past)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------


def test_revoke_blocks_download_and_online_verification(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-REV").json()
    _issue(client, att["attestation_id"])

    # 撤销需要原因。
    no_reason = client.post(
        f"/api/attestations/{att['attestation_id']}/revoke",
        json={"reason": ""},
        headers=MENTOR_HEADERS,
    )
    assert no_reason.status_code in (400, 422)

    revoked = client.post(
        f"/api/attestations/{att['attestation_id']}/revoke",
        json={"reason": "materials misstated"},
        headers=ADMIN_HEADERS,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    assert (
        client.get(
            f"/api/attestations/{att['attestation_id']}/package",
            headers=STUDENT_HEADERS,
        ).status_code
        == 409
    )
    # 重复撤销幂等：状态与原因不变。
    again = client.post(
        f"/api/attestations/{att['attestation_id']}/revoke",
        json={"reason": "again"},
        headers=ADMIN_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["status"] == "revoked"
    assert again.json()["revoke_reason"] == "materials misstated"

    # 离线校验码本身仍成立，但在线记录标记为已撤销。
    from app.core.attestation import verify_package

    session = TestSessionLocal()
    try:
        row = attestation_services.repo.get_by_id(session, att["attestation_id"])
        offline = verify_package(row.package)
        assert offline["checksum_ok"] is True
        online = attestation_services.verify_offline(session, row.package)
        assert online["valid"] is False
        assert online["online_record"]["revoked"] is True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 纠错 -> 替代证明链接旧编号；旧证明与原快照不变
# ---------------------------------------------------------------------------


def test_correction_issues_replacement_and_keeps_original_immutable(client, frozen_plan):
    pv = frozen_plan
    first = _apply(client, pv, request_id="REQ-V1").json()
    _issue(client, first["attestation_id"], valid_for_days=30)
    old_pkg = client.get(
        f"/api/attestations/{first['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    ).json()
    old_checksum = old_pkg["checksum"]

    # 迟到的纠错事件 + 新冻结。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-09",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 3600, "reason": "make-up"},
                }
            ]
        },
    )
    client.post(f"/api/plans/{pv}/freezes/F-02", json={})

    # 原冻结快照保持不变。
    f1 = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert f1["students"][0]["total_seconds"] == 7200

    second_apply = client.post(
        f"/api/plans/{pv}/attestations",
        json={
            "freeze_id": "F-02",
            "student_id": "S1",
            "purpose": "graduate admission corrected",
            "request_id": "REQ-V2",
        },
        headers=STUDENT_HEADERS,
    )
    assert second_apply.status_code == 201
    second = second_apply.json()

    # 不能替代一个 pending/他人 的证明。
    bad = _issue(
        client, second["attestation_id"], supersedes_id="ATT-NOPE"
    )
    assert bad.status_code == 400

    issued = _issue(
        client, second["attestation_id"], supersedes_id=first["attestation_id"]
    )
    assert issued.status_code == 201, issued.text
    new_body = issued.json()
    assert new_body["supersedes_id"] == first["attestation_id"]

    new_pkg = client.get(
        f"/api/attestations/{new_body['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    ).json()
    assert new_pkg["supersedes_id"] == first["attestation_id"]
    assert new_pkg["hours"]["total_seconds"] == 7200 + 3600
    assert new_pkg["source"]["freeze_id"] == "F-02"

    # 旧证明进入 superseded：停止下载，但其存储的数据包内容与校验码不变。
    old_meta = client.get(
        f"/api/attestations/{first['attestation_id']}", headers=STUDENT_HEADERS
    ).json()
    assert old_meta["status"] == "superseded"
    assert old_meta["checksum"] == old_checksum
    assert (
        client.get(
            f"/api/attestations/{first['attestation_id']}/package",
            headers=STUDENT_HEADERS,
        ).status_code
        == 409
    )
    session = TestSessionLocal()
    try:
        row = attestation_services.repo.get_by_id(session, first["attestation_id"])
        assert row.package["checksum"] == old_checksum  # 原数据包未被改写
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 离线核验与重启校验
# ---------------------------------------------------------------------------


def test_verify_endpoint_detects_tampering_and_accepts_clean_package(client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-VER").json()
    _issue(client, att["attestation_id"], valid_for_days=30)
    pkg = client.get(
        f"/api/attestations/{att['attestation_id']}/package",
        headers=STUDENT_HEADERS,
    ).json()

    # 核验接口对任何人开放（持有方可验，无需身份头）。
    ok = client.post("/api/attestations/verify", json={"package": pkg})
    assert ok.status_code == 200
    report = ok.json()
    assert report["valid"] is True
    assert report["checksum_ok"] is True
    assert report["online_record"]["known"] is True
    assert report["online_record"]["status"] == "issued"

    tampered = dict(pkg)
    tampered["hours"] = dict(pkg["hours"], total_seconds=999999)
    bad = client.post("/api/attestations/verify", json={"package": tampered})
    assert bad.json()["valid"] is False
    assert bad.json()["checksum_ok"] is False

    # 未知编号：离线校验仍可通过内容校验，但在线记录查无此证明。
    unknown = dict(pkg)
    unsigned = {k: v for k, v in unknown.items() if k != "checksum"}
    unsigned["attestation_id"] = "ATT-FORGED"
    unknown["attestation_id"] = "ATT-FORGED"
    unknown["checksum"] = domain.checksum_of(unsigned)
    forged = client.post("/api/attestations/verify", json={"package": unknown})
    body = forged.json()
    assert body["checksum_ok"] is True
    assert body["online_record"] == {"known": False}


def test_restart_self_check_recomputes_from_stored_rows(client, frozen_plan):
    pv = frozen_plan
    att = _apply(client, pv, request_id="REQ-RESTART").json()
    _issue(client, att["attestation_id"], valid_for_days=30)
    aid = att["attestation_id"]

    # 模拟进程重启：用同一数据库文件新建 engine 与会话工厂。
    restarted_engine = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    RestartedSession = sessionmaker(bind=restarted_engine)
    session = RestartedSession()
    try:
        report = attestation_services.restart_self_check(session, aid)
        assert report["valid"] is True, report
        assert report["checks"]["package_checksum"] is True
        assert report["checks"]["snapshot_binding"] is True
        assert report["checks"]["hours_derivable_from_freeze"] is True
        assert report["checks"]["mentor_chain_rebuilds"] is True

        # 重启后离线核验结果与签发时一致。
        row = attestation_services.repo.get_by_id(session, aid)
        offline = attestation_services.verify_offline(session, row.package)
        assert offline["valid"] is True
    finally:
        session.close()
        restarted_engine.dispose()


def test_self_check_fails_when_freeze_is_tampered(db, client, frozen_plan):
    att = _apply(client, frozen_plan, request_id="REQ-TAMPER").json()
    _issue(client, att["attestation_id"], valid_for_days=30)
    aid = att["attestation_id"]

    # 直接篡改底层冻结行（绕过服务），模拟存储损坏。
    from app.models import Freeze

    freeze_row = db.get(Freeze, (frozen_plan, "F-01"))
    corrupted = dict(freeze_row.snapshot)
    corrupted["required_seconds"] = 999999
    freeze_row.snapshot = corrupted
    db.commit()

    resp = client.get(
        f"/api/attestations/{aid}/self-check", headers=MENTOR_HEADERS
    )
    assert resp.status_code == 200
    report = resp.json()
    # generated_at 未变 -> source_checksum 仍一致，但学时派生必然对不上。
    assert report["valid"] is False
    assert report["checks"]["hours_derivable_from_freeze"] is False
