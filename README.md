# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 学时证明签发与核验

学员升学或就业需要机器可验证的学时证明时，只能从**已冻结的学期快照**中指定学员条目申请，数据包为最小披露：汇总学时、学时单元、达标结论、导师确认链（签到事件编号 → 确认事件编号 → 导师标识）与修正事件编号；不导出每日明细、签到时间区间、修正原因或其他学员数据。

数据包为自描述 JSON（`schema_version=attestation/v1`），包含用途（purpose）、签发时间、有效期（`expires_at` 或 `ttl_days`）、替代链（`supersedes`）以及两级完整性锚点：

* `checksum`：对除校验码外整个封包的规范化 SHA-256，离线即可验证是否被篡改；
* `source_fingerprint`：对来源冻结快照（含学员完整条目）的哈希，在线核验时由服务端从冻结数据重算比对，锚定冻结版本。

签发后数据包永不修改，冻结快照也不随新事件变化；后续纠错只能基于新冻结申请**替代证明**并通过 `supersedes_id` 链接旧编号，旧证明在替代证明签发时标记为 `superseded`（已撤销的旧证保持 `revoked` 终态）。

接口（身份通过 `X-Actor-Id`、`X-Actor-Role` 请求头传递，角色为 `student` / `mentor` / `admin`）：

| 操作 | 方法与路径 | 权限 |
| --- | --- | --- |
| 申请证明（按申请人+学员+方案+冻结+用途幂等） | `POST /api/plans/{pv}/freezes/{fid}/certificates/{cid}` | 学员仅本人；导师/管理员可代办 |
| 列出冻结下的证明 | `GET /api/plans/{pv}/freezes/{fid}/certificates` | 学员仅见本人相关 |
| 审批签发（生成数据包） | `POST /api/certificates/{cid}/approve` | 导师/管理员 |
| 驳回 | `POST /api/certificates/{cid}/reject` | 导师/管理员 |
| 撤销 | `POST /api/certificates/{cid}/revoke` | 仅管理员 |
| 查看元数据与审计链 | `GET /api/certificates/{cid}` | 学员仅本人相关 |
| 下载数据包（JSON 附件） | `GET /api/certificates/{cid}/download` | 同上 |
| 在线/离线核验 | `POST /api/certificates/verify` | 公开 |

核验接口入参二选一：`{"certificate_id": "..."}`（在线：读取登记状态并由冻结快照重算来源指纹）或 `{"package": {...}}`（离线：上传数据包，仅校验结构、校验码、有效期；本地恰有同编号登记时附带登记状态）。返回 `valid`、各项 `checks` 与失败 `reasons`；已撤销、已替代、已过期或非签发状态的证明均判定无效。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；学时证明部分覆盖角色权限范围、申请/审批/撤销的幂等、并发签发（条件更新保证仅一份数据包）、过期判定、撤销与替代链、冻结不变性、离线校验码核验以及重启后在线/离线复核；运行过程中不需要单独的数据库或网络服务。
