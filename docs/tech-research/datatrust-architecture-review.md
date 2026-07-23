# DataTrust 架构方案评审意见

> **评审对象**: [datatrust-architecture-design.md](./datatrust-architecture-design.md) **v1.3.1**  
> **日期**: 2026-07-23  
> **历次**: v1.0 → v1.1 → v1.2 → v1.3 → **v1.3.1**  
> **结论**: **审核通过，可以按分步计划开工。** 上次提出的阻塞/半阻塞项已基本关闭；仅剩已知接受的风险与清单勾选笔误，不挡 Phase 1。

---

## 1. 总评

| 维度 | 结论 |
|------|------|
| 架构 | **通过** |
| Phase 1 分步计划 | **通过** |
| v1.3 遗留 P2 | **多数已关闭**（双写、字段名、GraphRAG 表述、§9.3、JSONB、DataTrust 并行轨） |
| 是否可写代码 | **可以**：Step 1a / 1b / DataTrust 并行轨均可启动 |

v1.3.1 是一次合格的「评审闭环」修订：改的都是实现级正确性，没有引入新架构摇摆。

---

## 2. 上次开放项核对（v1.3 → v1.3.1）

| 上次问题 | v1.3.1 处理 | 状态 |
|----------|-------------|------|
| 双写顺序 submit→insert | §7.2 / Step 2 改为 mother → gate → **insert ES** → **submit**（失败 outbox） | ✅ 关闭 |
| 字段名错误 | `important_kwd` + `content_sm_ltks` | ✅ 关闭 |
| §9.3 多余 \`\`\` | 已移除（附录声明） | ✅ 关闭 |
| GraphRAG 表述歧义 | 统一为 Phase 1 保持 `progress>=1` 入队；Phase 3 事件驱动 | ✅ 策略已定 |
| DataTrust 不在 DAG | 增加并行轨说明，Step 4 前必须可用 | ✅ 关闭 |
| SQLite `JSONB` | 改为 `TEXT` | ✅ 关闭 |
| 行数硬上限 | 改为软上限 | ✅ 关闭 |
| 多 Worker + SQLite Outbox | **仍推荐 Phase 1 SQLite** | ⚠ 已知接受风险（见 §3） |

---

## 3. 残余问题（不挡开工，建议实现时注意）

### 3.1 清单勾选与 GraphRAG 策略仍不一致（文档笔误）

正文与风险表已明确：**Phase 1 允许 GraphRAG 在 pending 文本上跑**（接受半审风险）。

但 §13 仍勾选：

> [x] GraphRAG / RAPTOR 不在半审核数据上运行

这与 v1.3.1 策略矛盾。应改为未勾选，或改成「Phase 3 解决；Phase 1 已知风险已记录」。

### 3.2 Outbox 仍用 SQLite（有意识的取舍）

多 `task_executor`（`WS>1`）或无持久卷时，本地 `outbox.db` 仍可能分片/丢失。文档把 Redis 标为生产首选、Phase 1 用 SQLite——**可作为原型取舍**，但建议在 Step 1b 实现注释 / README 写明：

- 单机或 `WS=1`；或  
- 共享卷 + 单 Worker 重放  

否则上生产多副本前必须迁 Redis Stream。

### 3.3 小不一致（实现时顺手改）

| 点 | 说明 |
|----|------|
| §5.3 流程图 | 仍偏「submit 成败」叙述，未画出「先 insert」；以 §7.2 伪代码为准即可 |
| 术语表 fail-closed | 写「暂不入库」易误解；实际是 **入库但 `available_int=0`** |
| `important_tks` | 若原 chunk 有该字段，修正后宜与 `important_kwd` 一并重建 |
| embedding 元数据 | 协议仍无 `embd_id`；约定 Worker 经 `kb_id` 反查即可，建议在 §9 加一句 |
| Phase 0 清单 | 仍未勾「Outbox 选型 / GraphRAG Phase1」——策略已在正文写死，Phase 0 可把对应项标为已决议 |

### 3.4 dry-run 与 get_review_policy

`_gate_review` 在 interceptor 存在时跳过 HTTP 并全部 auto_pass——正确。注意实现时 **policy 查询与 submit 都要跳过**，不要只跳 submit。

---

## 4. 审核结论（针对「状态：待审核」）

| 审核项 | 结果 |
|--------|------|
| 总体架构与薄包装目标 | **通过** |
| 双态存储 / 门禁注入点 / Worker / outbox | **通过** |
| Phase 1 分步执行与验证门禁 | **通过** |
| 是否批准进入开发 | **批准** |

**建议动作**：

1. 改一下 §13 GraphRAG 勾选（笔误），不必为此再升大版本。  
2. 按 Step 1a ∥ 1b ∥ DataTrust API 开工。  
3. Step 2 严格按「先 insert 后 submit」实现。  
4. 多副本部署前再评估 Outbox 迁 Redis。

---

## 5. 版本对照（收束）

| 版本 | 结果 |
|------|------|
| v1.0 | 架构不可落地 |
| v1.1 | 可 Spike |
| v1.2 | 可 Phase 1（契约缺口） |
| v1.3 | 分步计划可用（P2 挂起） |
| **v1.3.1** | **设计审核通过，可开发** |

---

> 本轮起评审重点可转为：代码实现是否符合 §7.2 / Step 规格，以及 Step 4 九项 E2E 是否通过。
