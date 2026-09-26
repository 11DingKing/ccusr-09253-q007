"""请求者身份与权限范围。

通过请求头识别调用方，便于测试与网关对接：
- X-Actor-Id：调用方标识（学生学号 / 导师工号 / 管理员账号）
- X-Actor-Role：student / mentor / admin

生产环境通常由网关注入这些头；服务端只做资源级的范围校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header


STUDENT = "student"
MENTOR = "mentor"
ADMIN = "admin"
ROLES = frozenset({STUDENT, MENTOR, ADMIN})


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @property
    def is_mentor(self) -> bool:
        return self.role == MENTOR


def get_actor(
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Actor:
    if not x_actor_id or not x_actor_role:
        # 未提供身份头时按最小权限的匿名学生处理，具体资源访问仍会被拒。
        return Actor(actor_id="anonymous", role=STUDENT)
    role = x_actor_role.strip().lower()
    if role not in ROLES:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"unknown actor role '{x_actor_role}'")
    return Actor(actor_id=x_actor_id.strip(), role=role)
