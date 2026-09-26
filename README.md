# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 学时证明（attestation）

学生可基于**已冻结快照**申请升学/就业用学时证明。流程为：申请 → 导师/管理员审批签发 → 下载 →（可选）撤销/替代纠错。

请求通过 `X-Actor-Id` / `X-Actor-Role`（`student` / `mentor` / `admin`）头标识调用方：学生只能为本人申请并访问本人的证明，审批与撤销限导师/管理员，离线核验接口不要求身份。

| 接口 | 说明 |
| --- | --- |
| `POST /api/plans/{plan}/attestations` | 申请证明（body 含 `freeze_id`、`student_id`、`purpose`、`request_id`），`request_id` 幂等，重复请求返回同一编号（首次 201，重放 200） |
| `GET /api/plans/{plan}/students/{student}/attestations` | 列出本人证明 |
| `GET /api/attestations/{id}` | 证明元数据与状态 |
| `POST /api/attestations/{id}/approve` | 审批并签发；支持 `valid_for_days` 或 `expires_at`、`valid_from`、`supersedes_id` |
| `POST /api/attestations/{id}/reject` | 驳回（需原因） |
| `POST /api/attestations/{id}/revoke` | 撤销（需原因），重复撤销幂等 |
| `GET /api/attestations/{id}/package` | 下载最小披露数据包（仅签发态可下载） |
| `POST /api/attestations/verify` | 离线核验数据包（校验码、有效期、来源摘要、导师确认链；库中已知编号附在线撤销/替代状态） |
| `GET /api/attestations/{id}/self-check` | 重启/灾后自检（导师/管理员）：从落库快照与事件重算数据包自洽性 |

数据包设计要点：

- **最小披露**：只含该学生的汇总量（已确认/调整/总学时、学时单元、是否达标）与导师确认链，不含原始签到明细或其他学生。
- **冻结绑定**：包内含冻结 `plan_version`、`freeze_id`、`event_cutoff_id` 及来源摘要（SHA-256，规范 JSON）；证明签发后原快照与数据包永不改变。
- **校验码**：覆盖除 `checksum` 外的全部字段；任何篡改离线可检出。
- **有效期**：封入 `valid_from` / `expires_at`，离线即可判定未生效或过期。
- **纠错链**：错误只能通过基于新冻结快照签发**替代证明**纠正，新证明以 `supersedes_id` 链接旧编号，旧证明进入 `superseded` 终态但内容与校验码保持不变。

证明测试覆盖权限范围、申请/审批/撤销幂等、并发签发（仅一方成功）、过期与未生效、篡改检出、替代链与原快照不变性，以及跨进程重启后的重新自检。
