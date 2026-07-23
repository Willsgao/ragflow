# DataTrust — 数据可信审核平台架构设计文档

> **版本**: v1.3.1 | **日期**: 2026-07-23 | **状态**: 待审核
>
> **v1.3.1 修订说明**: 修正双写顺序（先 insert ES 后 submit）、统一字段名（important_kwd / content_sm_ltks）、修复 §9.3 格式笔误、对齐 GraphRAG Phase 1 策略描述。§11 Step DAG 补充 DataTrust 并行轨说明。

---

## 目录

1. [项目出发点与背景](#1-项目出发点与背景)
2. [项目目标与成功标准](#2-项目目标与成功标准)
3. [RAGFlow 现状分析](#3-ragflow-现状分析)
4. [遗漏维度补充分析](#4-遗漏维度补充分析)
5. [整体架构设计](#5-整体架构设计)
6. [Phase 0：审核对象澄清](#6-phase-0审核对象澄清)
7. [RAGFlow 侧改造方案](#7-ragflow-侧改造方案)
8. [DataTrust 侧设计方案](#8-datatrust-侧设计方案)
9. [数据流与协议](#9-数据流与协议)
10. [数据库 Schema 设计](#10-数据库-schema-设计)
11. [实施路线图](#11-实施路线图)
12. [风险评估与缓解](#12-风险评估与缓解)
13. [评审清单](#13-评审清单)
14. [附录](#14-附录)

---

## 1. 项目出发点与背景

RAGFlow 依赖 LLM 对文档进行深度解析并提取数据存入检索引擎（ES/Infinity）。LLM 提取存在幻觉、偏差、遗漏等问题，且无人工校验环节。DataTrust 作为独立审核平台，对 LLM 产出数据进行人工审核后再入生产库。

**设计哲学**：薄包装原则——RAGFlow 改动尽可能小，DataTrust 完全独立部署、可插拔，不可用时有明确降级行为。

---

## 2. 项目目标与成功标准

| 目标 | 描述 | 衡量标准 |
|------|------|----------|
| **人审入库** | LLM 提取数据经人工审核后进入生产检索库 | 审核覆盖率 ≥ 可配置 |
| **双态存储** | 待审数据不可检索（available_int=0），审核通过后可检索（=1） | 零字段新增，复用现有机制 |
| **审核结果应用** | 审核完成后 Worker 自动应用修正、重算 embedding、启用检索 | 全自动，无人工触发 |
| **反馈闭环** | 修正数据 → Few-shot → Prompt 迭代 | 提取准确率持续提升 |
| **可插拔降级** | DataTrust 不可用时可配置 fail-closed/fail-open | 审计日志可追溯 |

**非目标**：不改造文件解析流程、不实现实时审核（异步）、审核 UI 在 DataTrust 侧。

---

## 3. RAGFlow 现状分析

### 3.1 默认执行路由（已代码确认）

```python
# rag/svr/task_executor.py:1699
run_mode = os.environ.get("TE_RUN_MODE", "0")  # 默认 "0"
```

| `TE_RUN_MODE` | 路径 | 入口 |
|---------------|------|------|
| `"0"`（**默认**） | **refactor 版本** | `TaskManager.run_refactored_task()` |
| `"1"` | 新旧对比 | `do_handle_task()` + dry-run |
| 其他 | 原始版本 | `do_handle_task()` |

**v1.0 错误**：以旧 `task_executor.py` 为主改动点。**正：注入点挂在 `task_executor_refactor/`。**

### 3.2 统一写入层（chunk_service.py）

```python
# rag/svr/task_executor_refactor/chunk_service.py
async def insert_chunks(self, task_id, tenant_id, dataset_id, chunks, doc_bulk_size=None):
    mothers = self._create_mother_chunks(chunks)      # line 282
    await self._insert_mother_chunks(...)              # line 285
    return await self._insert_main_chunks(...)         # line 287

async def _intercept_doc_store_insert(self, chunks, index_name, task_dataset_id):  # line 347
    if self._task_context.write_interceptor:
        return self._task_context.write_interceptor.intercept("docStoreConn.insert")
    else:
        return await thread_pool_exec(settings.docStoreConn.insert, chunks, index_name, task_dataset_id)
```

调用链：`task_handler.py` → `chunk_service.insert_chunks()` → `_insert_main_chunks()` → `_intercept_doc_store_insert()` → `settings.docStoreConn.insert()`。**所有标准路径的 chunk 写入最终汇聚于此。**

### 3.3 Chunk 真实字段与 available_int

标准路径产出**文本 chunk**（`rag/utils/ob_conn.py:68-78`），核心字段：

| 字段 | 说明 |
|------|------|
| `content_with_weight` | 带权重正文 |
| `available_int` | **已存在**：0=不参与检索（mother chunk/TOC），1=正常 |
| `mom_id` | mother chunk ID |
| `*_vec` | embedding 向量 |

**关键发现**：
- ❌ 没有 `confidence` 字段
- ❌ 没有 `extracted_fields` 结构
- ✅ `available_int` 语义完美匹配审核门禁：0=待审不可检索，1=审核通过可检索

现有用法：mother chunk 和 TOC chunk 设 `available_int=0`，检索时 `WHERE available_int=1`。审核门禁**零新增字段**。

### 3.4 GraphRAG 写入

`rag/graphrag/utils.py:53-112` — `insert_chunks_bounded()` 批量 64、并发 4、重试 3 次，底层调用 `settings.docStoreConn.insert()`。社区报告/实体在索引完成后独立写入。

### 3.5 Canvas 组件系统

`agent/canvas.py` 中 `userfillup` 共 4 处硬编码（第 420、474、480、664-671 行），`is_resume` 用 `self.path[0].lower().find("userfillup")`。**v1.0 "一行改动"不成立**，且组件内轮询与事件驱动 resume 模型冲突。

---

## 4. 遗漏维度补充分析

🔴 **高优先级**：反馈闭环（修正→Few-shot→Prompt迭代）、增量更新冲突（版本链+默认overwrite）、数据下架（复用available_int=0）

🟡 **中优先级**：抽样审核（不同审核对象不同策略）、派生数据审核（挂"全文审核完成"之后）、审核员质控（金标准+准确率+双盲）

🟢 **较低优先级**：数据血缘（JSON扩展lineage）、数据生命周期（TTL策略）

---

## 5. 整体架构设计

### 5.1 架构全景图（修正版）

```
RAGFlow
  │
  task_executor.py ──(TE_RUN_MODE=0 默认)──▶ task_executor_refactor/
                                                   │
                                            task_handler.py
                                                   │
  ┌────────────────────────────────────────────────▼──────────────────────┐
  │  chunk_service.py (统一写入层)                                         │
  │                                                                       │
  │  insert_chunks()                                                      │
  │    ├── _create_mother_chunks() ──→ mothers (对全量, 先于分流)           │
  │    ├── [🔴 注入点] _gate_review() ──HTTP──▶ DataTrust                  │
  │    │       ├── auto_pass → available_int=1 → 正常写入 ES/Infinity     │
  │    │       └── pending   → available_int=0 → 写入但不可检索            │
  │    │              └── submit 失败 ──→ outbox 暂存                      │
  │    └── _insert_main_chunks() → ES/Infinity                            │
  └───────────────────────────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────────────────────────┐
  │  ReviewResultWorker (新增，后台定时任务)                                │
  │                                                                       │
  │  ┌─ 职责 A: 拉取审核结果                                              │
  │  │   30s 轮询 DataTrust → 拉取 completed batches →                    │
  │  │   → 全字段更新(content+分词+向量) → available_int=1 → 可检索       │
  │  │                                                                   │
  │  └─ 职责 B: 重放 outbox                                              │
  │      扫描 outbox.db → 退避重放 submit → 成功删记录 / 失败告警         │
  └───────────────────────────────────────────────────────────────────────┘

  ES/Infinity: available_int=1 → 可检索; available_int=0 → 不可检索
```

### 5.2 降级策略（修正：fail-closed 可配置）

| 策略 | 行为 | 场景 |
|------|------|------|
| `fail_closed`（**默认**） | DataTrust不可用→available_int=0，待恢复补审 | 合规优先 |
| `fail_open` | DataTrust不可用→available_int=1，标记auto_passed | 时效优先 |

两种均记录审计日志，fail_open 下事后可追溯补偿审核。

### 5.3 fail-closed 补审机制（Outbox 模式，v1.2 新增）

当 DataTrust 熔断/不可达时，`_gate_review()` 按 `fail_closed` 策略返回全部 pending，此时 **无法调用 `POST /review-batches`**。结果：

- ES/Infinity 已有 `available_int=0` 的 chunk
- DataTrust **没有**对应的 batch/task 记录
- 服务恢复后 Worker **捞不到这些孤儿 chunk**

**解法：本地 Outbox 队列**

```
                        ┌─ submit 成功 ──→ DataTrust batch 正常创建
gate_review() ── pending ┤
                        └─ submit 失败 ──→ 写入本地 outbox 表/文件 ──→ 返回 available_int=0
                                                      │
                                              服务恢复后定时扫描
                                                      │
                                              重放 submit → DataTrust
                                                      │
                                              成功后删除 outbox 记录
```

**Outbox 实现选项**（Phase 0 选定）：

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| SQLite 本地文件 | 零外部依赖，原子写入 | 需保证文件不丢失 | 单机/小规模 |
| Redis Stream | 天然持久化 + 消费组 | 依赖 Redis 可用 | 生产环境首选 |
| PostgreSQL（复用 DataTrust 恢复） | 数据不丢 | 依赖 DataTrust 恢复后才能写入 | 最可靠但循环依赖 |

**推荐**：Phase 1 用 **SQLite 本地文件**（单文件，零运维），Phase 2+ 迁到 Redis Stream。

**Outbox Schema**（SQLite 版）：

```sql
CREATE TABLE review_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,           -- JSON 序列化的 submit 请求体
    status VARCHAR(16) DEFAULT 'pending',  -- pending/submitted/failed
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 10,
    next_retry_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**重放策略**：

1. ReviewResultWorker **同时负责两件事**：轮询已完成的审核批次 + 重放 outbox 中的待提交记录
2. 重放间隔：初始 30s，指数退避（30s → 1m → 2m → 4m → ...），最大 10 分钟
3. 超过 `max_retries` 仍未成功 → 标记 `failed` + 告警
4. 重放幂等：submit 成功但响应丢失 → batch 去重（以 `doc_id + chunk_id + data_version` 为幂等键）

**审计追溯**：outbox 表不删 `failed` 记录，人工可查看并手动重放。

---

## 6. Phase 0：审核对象澄清（v1.1 新增）

v1.0 将三种审核对象混为一谈，API 协议和策略引擎悬空。必须区分：

### 类型 A：文本 Chunk（Phase 1 范围）

- **来源**：标准文档解析（PDF/Word→切分→文本chunk）
- **真实 Schema**：`content_with_weight`, `available_int`, `*_vec` 等
- **特征**：无 `confidence`，无 `extracted_fields`
- **审核方式**：全文审核（阅读文本，判断切分/内容是否正确）
- **策略**：auto_pass / full_review / sampled（纯随机抽样，不依赖字段级置信度）

### 类型 B：结构化抽取结果（Phase 3+）

- **来源**：Extractor 组件 / 自定义流水线产出
- **Schema**：含 `extracted_fields`（字段名→{value, confidence, data_type}）
- **特征**：有字段级置信度
- **策略**：低置信度必审 + 关键字段(data_type=money/date)必审 + 抽样

### 类型 C：派生摘要/实体（Phase 3+）

- **来源**：GraphRAG 社区报告、RAPTOR 层级摘要
- **特征**：依赖源 chunks，含 `source_chunks` 引用
- **策略**：源 chunks 全部审核通过后才生成并送审

**Phase 1 只做类型 A**。B/C 独立排期。

---

## 7. RAGFlow 侧改造方案

### 7.1 改动清单

| 文件 | 说明 |
|------|------|
| `rag/svr/task_executor_refactor/chunk_service.py` | **核心门禁**：`insert_chunks()` 入口注入 |
| `rag/svr/data_trust_client.py` | **新增**：HTTP 客户端 + 熔断器 |
| `rag/svr/review_result_worker.py` | **新增**：审核结果轮询 + re-embed + available_int 更新 |
| `agent/canvas.py` + `review_checkpoint.py` | **Phase 3+**：事件驱动 Canvas 审核组件 |

### 7.2 chunk_service.py 门禁注入

在 `insert_chunks()` 方法中注入门禁。**关键顺序**：先对全量 chunks 建 mother / 关联 `mom_id`，再按策略分流设 `available_int`。门禁只改可见性，不拆开 mother 流水线。

```python
# 注入逻辑伪代码（修正后）
async def insert_chunks(self, task_id, tenant_id, dataset_id, chunks, ...):
    # Step 1: 先对全量 chunks 建 mother（保持原有 mother 流水线完整）
    mothers = self._create_mother_chunks(chunks)
    await self._insert_mother_chunks(...)
    
    # Step 2: 门禁分流 —— 仅影响 available_int 和是否提交审核
    auto_pass, pending = await self._gate_review(chunks, tenant_id, dataset_id)
    
    # Step 3: 先写入 ES——auto_pass 设 available_int=1, pending 设 0
    auto_pass_with_flag = [{**c, "available_int": 1} for c in auto_pass]
    pending_with_flag  = [{**c, "available_int": 0} for c in pending]
    all_result_chunks = auto_pass_with_flag + pending_with_flag
    await self._insert_main_chunks(all_result_chunks, ...)
    
    # Step 4: 后提交审核（先落地再通知，避免 DataTrust 反向孤儿）
    if pending:
        await self._submit_review_batch(pending, tenant_id, dataset_id, task_id)
        # 成功 → DataTrust 收到 batch
        # 失败 → outbox.enqueue()（熔断 / 网络异常 / 超时）

# 门禁核心逻辑
async def _gate_review(self, chunks, tenant_id, kb_id):
    client = DataTrustClient(...)
    try:
        policy = await client.get_review_policy(kb_id)
    except CircuitBreakerOpenError:
        strategy = os.environ.get("DATATRUST_FAIL_STRATEGY", "fail_closed")
        if strategy == "fail_open":
            return chunks, []    # 全部 auto_pass
        return [], chunks        # 全部 pending (fail_closed)
    
    if policy.mode == "auto_pass":   return chunks, []
    if policy.mode == "full_review": return [], chunks
    if policy.mode == "sampled":
        # 确定性哈希抽样（同一chunk总得相同结果）
        auto, pending = [], []
        for c in chunks:
            seed = int(hashlib.md5(c["id"].encode()).hexdigest()[:8], 16)
            if random.Random(seed).random() < policy.sample_rate:
                pending.append(c)
            else:
                auto.append(c)
        return auto, pending
    return chunks, []  # 默认 auto_pass
```

### 7.3 熔断器（data_trust_client.py）

- CLOSED → 正常调用
- 连续 3 次失败 → OPEN → 按 `DATATRUST_FAIL_STRATEGY` 执行（fail_closed 全 pending / fail_open 全通过），30s 后 → HALF_OPEN
- HALF_OPEN 成功 → CLOSED；失败 → OPEN（重新计时）
- 进入 OPEN 状态时，pending chunks 走 outbox（§5.3）暂存

### 7.4 dry-run 防护（v1.2 新增）

`TE_RUN_MODE=1`（对比模式）下 `task_executor` 会同时运行新旧两条路径并对比结果。此时 `write_interceptor` 会拦截真实写入，但门禁 HTTP 调用仍会污染 DataTrust。

**防护**：`_gate_review()` 检测 `self._task_context.write_interceptor` 存在时，跳过 HTTP 调用，直接返回 `chunks, []`（全部 auto_pass）。dry-run 模式不产生审核副作用。

### 7.5 审核结果 Worker（review_result_worker.py）

Worker 承担 **两项职责**：轮询应用审核结果 + 重放 outbox 待提交记录。

**职责 1：应用审核结果**

定时 30s 轮询 DataTrust，拉取已完成审核批次：

```
拉取 completed batches 列表（含 tenant_id、doc_id、kb_id）
  ↓
逐批次获取审核结果
  ↓
approved              → available_int=1
approved_with_changes → 全字段更新（见下方）→ 重算 embedding → available_int=1
rejected              → available_int 保持 0，记录原因
  ↓
标记批次为 applied（幂等去重）
```

**修正后全字段同步清单**（不只是改 `content_with_weight`）：

| 字段 | 变更方式 | 说明 |
|------|----------|------|
| `content_with_weight` | 替换为 `corrected_content` | 审核修正后的正文 |
| `content_ltks` | **重新分词** | 正文变了，全文检索分词必须重建 |
| `content_sm_ltks` | **重新提取** | 短记忆分词同样需要重建 |
| `important_kwd` | 重新提取 | 关键词集合需要重建 |
| `q_*_vec` | **重新 embedding** | 修正文本的向量重新计算 |
| `available_int` | 0 → 1 | 审核通过，可检索 |

Worker 写入依赖 `tenant_id` 构造索引名 `index_name(tenant_id)`，因此协议必须冗余该字段（§9.2）。

**职责 2：Outbox 重放**

Worker 同时扫描本地 outbox 表中的 `pending` 记录，按退避策略重放提交（见 §5.3）。成功 → 删记录；超 max_retries → 告警。

### 7.6 Canvas 集成（Phase 3+，事件驱动）

改为：组件写 checkpoint → 提交审核任务 → yield 暂停。审核完成后 DataTrust webhook 回调 RAGFlow 触发 resume。非轮询，不占用 worker。

---

## 8. DataTrust 侧设计方案

### 8.1 技术栈与模块

- FastAPI + PostgreSQL + React + Ant Design + Redis
- 模块：`api/`（review_tasks, policies, admin）、`core/`（strategy_engine, quality_control, feedback_loop）、`services/`（review_service, notification）、`web/`（React 审核工作台）

### 8.2 策略引擎（Phase 1 简化）

Phase 1 只处理文本 chunk，无字段级置信度：

```python
class ReviewPolicy:
    mode: str           # auto_pass | full_review | sampled
    sample_rate: float  # 抽样比例（仅 sampled 模式）

class StrategyEngine:
    def evaluate(self, chunk, doc_meta) -> bool:
        # 返回 True = 需要审核
        if policy.mode == "auto_pass":   return False
        if policy.mode == "full_review": return True
        if policy.mode == "sampled":
            return deterministic_hash_sample(chunk["id"], policy.sample_rate)
        return False
```

Phase 3+ 升级为完整的字段级策略引擎（面向类型 B）。

### 8.3 反馈闭环（Phase 4+）

审核修正 → Few-shot 聚合（按文档/字段类型）→ Prompt 版本管理 → A/B 灰度验证 → Prompt 升级 → RAGFlow 同步。

---

## 9. 数据流与协议

### 9.1 提交审核

```
POST /api/v1/review-batches
{
  "tenant_id": "tenant_001",                               # [v1.2 新增] 必须
  "doc_id": "doc_001", "kb_id": "kb_001",
  "object_type": "text_chunk",
  "chunks": [{
    "id": "chunk_abc",
    "content_with_weight": "合同第一条...",
    "page_num_int": [1]
  }]
}
→ { "batch_id": "batch_001", "status": "pending", "chunk_count": 50 }
```

### 9.2 拉取完成的批次（Worker 轮询）

```
GET /api/v1/review-batches?status=completed&kb_id=kb_001
→ [{
  "batch_id": "batch_001", "tenant_id": "tenant_001",     # [v1.2 新增]
  "doc_id": "doc_001", "kb_id": "kb_001",
  "completed_at": "...", "chunk_count": 50
}]
```

### 9.3 获取批次详情

```
GET /api/v1/review-batches/batch_001/result
→ {
  "batch_id": "batch_001",
  "tenant_id": "tenant_001",                              # [v1.2 新增]
  "kb_id": "kb_001", "doc_id": "doc_001",
  "status": "completed",
  "chunks": [
    { "id": "chunk_abc", "action": "approved" },
    { "id": "chunk_def", "action": "approved_with_changes",
      "corrected_content": "合同第一条：甲方向乙方【采购】...",
      "changes": [{ "field": "content", "old": "...", "new": "..." }] },
    { "id": "chunk_ghi", "action": "rejected", "reason": "与原文档无关" }
  ]
}
```

### 9.4 审核策略查询

```
GET /api/v1/policies/{kb_id}
→ { "kb_id": "kb_001", "mode": "sampled", "sample_rate": 0.2 }
```

---

## 10. 数据库 Schema 设计

### 10.1 DataTrust 核心表

```sql
-- 原始文档提取数据表
CREATE TABLE documents_raw (
    id UUID PRIMARY KEY,
    ragflow_doc_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(64) NOT NULL,
    chunk_id VARCHAR(128) NOT NULL,
    object_type VARCHAR(32) DEFAULT 'text_chunk',  -- text_chunk/structured_extraction/derived_summary
    original_content TEXT NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',           -- pending/reviewing/approved/approved_with_changes/rejected/withdrawn
    corrected_content TEXT,
    reject_reason TEXT,
    data_version INT DEFAULT 1,
    parent_version_id VARCHAR(64),
    lineage JSONB,                                  -- {model, version, extraction_time, reviewer, reviewed_at, applied_at}
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 审核批次表
CREATE TABLE review_batches (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,                 -- [v1.2] Worker 回写 ES 索引名必需
    kb_id VARCHAR(64) NOT NULL,
    doc_id VARCHAR(64) NOT NULL,
    object_type VARCHAR(32) DEFAULT 'text_chunk',
    status VARCHAR(32) DEFAULT 'pending',           -- pending/in_progress/completed/applied
    total_count INT DEFAULT 0,
    completed_count INT DEFAULT 0,
    assigned_to VARCHAR(64),                        -- 审核员
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

-- 审核任务表（批次下的单个chunk）
CREATE TABLE review_tasks (
    id UUID PRIMARY KEY,
    batch_id UUID REFERENCES review_batches(id),
    chunk_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',           -- pending/assigned/completed
    decision VARCHAR(32),                           -- approved/approved_with_changes/rejected
    original_content TEXT NOT NULL,
    corrected_content TEXT,
    changes JSONB,                                  -- [{field, old_value, new_value}]
    reject_reason TEXT,
    reviewer_id VARCHAR(64),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 审核策略配置表
CREATE TABLE review_policies (
    id UUID PRIMARY KEY,
    kb_id VARCHAR(64) NOT NULL UNIQUE,
    mode VARCHAR(32) DEFAULT 'auto_pass',           -- auto_pass/full_review/sampled
    sample_rate FLOAT DEFAULT 0.2,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 审核员质控表 (Phase 3+)
CREATE TABLE reviewer_quality_metrics (
    id UUID PRIMARY KEY,
    reviewer_id VARCHAR(64) NOT NULL,
    total_reviewed INT DEFAULT 0,
    golden_standard_passed INT DEFAULT 0,
    golden_standard_total INT DEFAULT 0,
    accuracy FLOAT,
    avg_review_duration_seconds FLOAT,
    fatigue_score FLOAT,
    last_active_at TIMESTAMP
);

-- 金标准数据表 (Phase 3+)
CREATE TABLE golden_standards (
    id UUID PRIMARY KEY,
    content TEXT NOT NULL,
    expected_decision VARCHAR(32) NOT NULL,         -- approved/approved_with_changes/rejected
    expected_changes JSONB,
    difficulty VARCHAR(16) DEFAULT 'medium',
    usage_count INT DEFAULT 0
);

-- Prompt 版本管理表 (Phase 4+)
CREATE TABLE prompt_versions (
    id UUID PRIMARY KEY,
    kb_id VARCHAR(64),
    doc_type VARCHAR(64),
    version VARCHAR(32) NOT NULL,
    prompt_template TEXT NOT NULL,
    few_shot_examples JSONB,
    status VARCHAR(16) DEFAULT 'draft',             -- draft/testing/active/archived
    activated_at TIMESTAMP
);
```

### 10.2 RAGFlow 侧零字段新增

审核门禁**不需要**为 RAGFlow 的 `documents` 表或 chunk schema 新增任何字段。所有审核状态在 DataTrust PostgreSQL 中管理。`available_int` 已足矣：

- `0` = 待审核 / 审核驳回 / mother chunk / TOC（不参与检索）
- `1` = 审核通过 / auto_pass（可检索）

### 10.3 `available_int=0` 语义边界（v1.2 新增）

`available_int=0` 被多个对象共用，**不可**通过 `available_int=0` 的聚合推断审核状态：

| 对象 | `available_int` | 语义 | 来源 |
|------|-----------------|------|------|
| mother chunk | 0 | 摘要母块，不可检索 | `chunk_service._create_mother_chunks()` |
| TOC chunk | 0 | 目录块，不可检索 | 解析流程 |
| GraphRAG entity/relation | 0 | 图谱专用，常规检索过滤 | `graphrag/utils.py` |
| **待审文本 chunk** | **0** | **审核中，暂不可检索** | **DataTrust 门禁** |
| **审核驳回 chunk** | **0** | **被驳回，不可检索** | **Worker 保持 0** |
| fail-closed chunk | 0 | 暂存，待补审 | 熔断 outbox |
| 正常可检索 chunk | 1 | 审核通过 / auto_pass | 默认 / Worker 更新 |

**关键约束**：

- ❌ **不能**用 `SELECT COUNT(*) WHERE available_int=0` 统计待审数量
- ❌ **不能**用扫 ES 的 `available_int=0` 做 fail-closed 恢复
- ✅ 审核状态权威源 = **DataTrust PostgreSQL + 本地 outbox**
- ✅ `available_int` **仅负责检索可见性**，不作状态追踪用途
- ✅ 文档「全文审核完成」事件依赖 DataTrust 批次状态，不依赖 `available_int` 聚合

### 10.4 文档状态策略（v1.2 新增）

**问题**：RAGFlow `documents.run` 状态为 `DONE` 时表示解析完成，但 chunk 可能大量 pending（`available_int=0`）。用户看到 `DONE` 以为可检索，实际搜不到。

**策略**（Phase 0 选定其一）：

| 方案 | 描述 | 侵入性 | 推荐度 |
|------|------|--------|--------|
| **方案 1**：UI 聚合 DataTrust 状态 | RAGFlow 零字段新增；前端同时查 DataTrust `/policies/{kb_id}/doc/{doc_id}/status` | 无 | ⭐⭐⭐ |
| 方案 2：扩展 `progress_msg` | `documents.progress_msg` 追加 "审核中 (15/50)" | 低（复用现有字段） | ⭐⭐ |
| 方案 3：阻塞 DONE | 存在 pending chunk 时不设 DONE，阻 GraphRAG 入队 | 中（改状态机） | ⭐ |

**推荐方案 1**（Phase 1—Phase 2）：

- DataTrust 提供 `GET /api/v1/docs/{doc_id}/review-status` 返回 `{ total, reviewed, approved, rejected }`
- RAGFlow 前端在文档详情页异步查询该端点
- 文档列表页：解析中 → `RUNNING`，解析完 → `DONE`（不变），审核状态作为附加 badge 展示

**GraphRAG 入队时机**（与文档状态联动）：

- 当前逻辑：解析 `progress>=1` 即可入队 GraphRAG
- **Phase 1**：保持原样，`progress>=1` 即可入队（不等待审核完成）
- **Phase 3**：改为 DataTrust batch `applied` → 发 Redis 事件 → `queue_raptor_o_graphrag_tasks`（事件驱动）

---

## 11. 实施路线图（修正版）

### Phase 0：设计澄清（1周，先于任何代码）

- [ ] 确认三种审核对象 schema，与 RAGFlow 实际产出对齐
- [ ] 确认降级默认策略（fail-closed/fail-open）与产品团队达成一致
- [ ] ReviewResultWorker 的嵌入方式（独立进程 vs 定时任务 vs RAGFlow 内协程）
- [ ] Outbox 实现选型与 GraphRAG Phase 1 策略定稿（已在正文决议：SQLite / progress>=1 入队）
- [ ] 输出评审清单全部勾选后的确认版本

### Phase 1：标准路径门禁（3-4周）

- [ ] DataTrust：FastAPI 骨架 + PostgreSQL Schema + 审核任务 CRUD API
- [ ] DataTrust：策略管理 API + 简化策略引擎
- [ ] RAGFlow：`data_trust_client.py`（熔断器 + HTTP 客户端）
- [ ] RAGFlow：`chunk_service.py` 注入门禁逻辑
- [ ] RAGFlow：`review_result_worker.py`（审核结果拉取 + re-embed + available_int 更新）
- [ ] 集成测试：auto_pass / full_review / sampled / 服务不可用(fail_closed) / 服务不可用(fail_open) 五种行为可测
- [ ] pending chunk 不进入检索结果验证

#### Phase 1 分步执行计划

RAGFlow 侧的 Phase 1 开发按**依赖分层、逐步交付、人工把关**推进。每步产出独立可验证的代码，通过后人工 `git commit`，再进入下一步。

```
Step 1a                 Step 1b
data_trust_client.py    review_outbox.py
  (~80 行)               (~80 行)
  HTTP 客户端 + 熔断器     SQLite outbox 队列
      │                       │
      └───────┬───────────────┘
              │ 无相互依赖，可并行开发
              ▼
        Step 2
        chunk_service.py（改动 ~100 行）
        门禁注入：_gate_review() + _submit_review_batch()
              │
              ▼
        Step 3
        review_result_worker.py（~200 行）
        双职责 Worker：轮询审核结果 + outbox 重放
              │
              ▼
        Step 4
        集成验证
        端到端行为验证 + 回归检查
```

> **并行轨**：Step 1-4 是 RAGFlow 侧开发。DataTrust 侧 API 开发（FastAPI 骨架 + PostgreSQL Schema + 策略 CRUD）与之并行推进，Step 4 集成验证前 DataTrust 必须可用。

**Step 1a：`data_trust_client.py`**（无前置依赖）

| 项目 | 内容 |
|------|------|
| **文件** | `rag/svr/data_trust_client.py`（新增） |
| **规模** | ~80 行（软上限） |
| **职责** | HTTP 客户端封装 + 熔断器 |
| **关键接口** | `get_review_policy(kb_id)` → `ReviewPolicy`；`submit_review_batch(payload)` → `batch_id` |
| **熔断逻辑** | 连续 3 次失败 → OPEN(30s) → HALF_OPEN；OPEN 时抛 `CircuitBreakerOpenError` |
| **环境变量** | `DATATRUST_URL`, `DATATRUST_FAIL_STRATEGY`, `DATATRUST_CIRCUIT_THRESHOLD`, `DATATRUST_CIRCUIT_RECOVERY` |
| **可验证** | 单元测试：正常响应 / 超时 / 服务不可达 / 熔断开闭状态切换 |

**Step 1b：`review_outbox.py`**（无前置依赖，可与 1a 并行）

| 项目 | 内容 |
|------|------|
| **文件** | `rag/svr/review_outbox.py`（新增） |
| **规模** | ~80 行（软上限） |
| **职责** | SQLite 本地 outbox 队列管理 |
| **关键接口** | `enqueue(payload)` → `record_id`；`peek_pending(limit)` → `[record]`；`mark_submitted(record_id)` / `mark_failed(record_id)` |
| **Schema** | 见 §5.3 `review_outbox` 表结构 |
| **幂等键** | `doc_id + chunk_id + data_version`，enqueue 前检查去重 |
| **环境变量** | `REVIEW_OUTBOX_PATH`, `REVIEW_OUTBOX_MAX_RETRIES` |
| **可验证** | 单元测试：写入→读取→标记→去重 |

**Step 2：`chunk_service.py` 门禁注入**（依赖 Step 1a + 1b）

| 项目 | 内容 |
|------|------|
| **文件** | `rag/svr/task_executor_refactor/chunk_service.py`（修改） |
| **规模** | 改动 ~100 行（软上限，实际以功能完成为准） |
| **职责** | 在 `insert_chunks()` 中注入审核门禁 |
| **新增方法** | `_gate_review(chunks, tenant_id, kb_id)` → `(auto_pass, pending)`；`_submit_review_batch(pending, ...)` → 调用 DataTrust + outbox fallback |
| **关键顺序** | mother → gate → insert ES (avail=0) → submit DataTrust → 失败走 outbox（见 §7.2 伪代码） |
| **dry-run 防护** | 检测 `self._task_context.write_interceptor` → 跳过 HTTP 调用，全部 auto_pass |
| **熔断衔接** | `_gate_review()` 捕获 `CircuitBreakerOpenError` → 按 `DATATRUST_FAIL_STRATEGY` 分流；fail_closed 时 chunk 已落地 ES（avail=0），`_submit_review_batch()` 失败 → `outbox.enqueue()` |
| **可验证** | 单元测试：auto_pass / full_review / sampled 三种策略分流正确性；dry-run 不调用 HTTP；mock DataTrust 不可达时的降级行为 |

**Step 3：`review_result_worker.py`**（依赖 Step 1 + 2）

| 项目 | 内容 |
|------|------|
| **文件** | `rag/svr/review_result_worker.py`（新增） |
| **规模** | ~200 行（软上限：含全字段同步 + embedding + ES 更新 + outbox 重放，实际可能更多） |
| **职责** | 双职责后台任务 |
| **职责 A** | 30s 轮询 DataTrust `GET /review-batches?status=completed` → 拉取批次详情 → 全字段同步（content + 重分词 + 重 embedding）→ `available_int=1` |
| **职责 B** | 扫描 outbox `pending` 记录 → 指数退避重放 `submit_review_batch()` → 成功删记录 / 超 max_retries 告警 |
| **幂等** | 批次级 `applied` 标记，防止重复应用审核结果 |
| **全字段同步** | 见 §7.5 表格：`content_with_weight` 替换 + `content_ltks` / `content_sm_ltks` 重新分词 + `important_kwd` 重建 + `q_*_vec` 重新 embedding |
| **可验证** | 单元测试：completed batch 正确应用 / outbox 重放成功与失败路径 / 幂等去重 |

**Step 4：集成验证**（依赖全部 Step）

| 验证项 | 方法 | 通过标准 |
|--------|------|----------|
| **策略 auto_pass** | 配置 `mode=auto_pass`，解析文档 | chunk `available_int=1`，可直接检索 |
| **策略 full_review** | 配置 `mode=full_review`，解析文档 | chunk `available_int=0`，不可检索；DataTrust 收到 batch |
| **策略 sampled** | 配置 `mode=sampled, sample_rate=0.5` | 约 50% chunk 待审，哈希确定性（同一 chunk 重复结果一致） |
| **熔断 fail_closed** | 停掉 DataTrust，解析文档 | chunk `available_int=0`，outbox 有记录，日志有审计 |
| **熔断 fail_open** | 设 `DATATRUST_FAIL_STRATEGY=fail_open`，停 DataTrust | chunk `available_int=1`，标记 auto_passed |
| **审核通过回写** | DataTrust 标记 batch completed (approved) | Worker 自动设 `available_int=1`，可检索 |
| **审核修正回写** | DataTrust 标记 approved_with_changes | Worker 更新 content + 重分词 + 重 embedding，可检索 |
| **outbox 重放** | 熔断期有 outbox 记录，恢复 DataTrust | Worker 自动重放提交，DataTrust 收到 batch |
| **回归检查** | 关闭 DataTrust 功能，`mode=auto_pass` | 行为与原始 RAGFlow 一致，无性能劣化

### Phase 2：审核工作台 + 状态管理（2-3周）

- [ ] DataTrust：React 审核工作台 UI
- [ ] DataTrust：审核任务分配与状态流转
- [ ] 文档生命周期：UI 显示 `pending_review` 状态
- [ ] 数据下架：复用 `available_int=0` 做软下架
- [ ] 增量更新冲突策略落地（默认 overwrite）

### Phase 3：扩展路径（3-4周）

- [ ] Canvas ReviewCheckpoint（事件驱动，非轮询挂起）
- [ ] 类型 B（结构化抽取）门禁
- [ ] GraphRAG / RAPTOR 挂"全文审核完成"之后
- [ ] 审核员质控（金标准混入 + 准确率追踪）

### Phase 4：反馈闭环（2-3周）

- [ ] 反馈闭环完整链路
- [ ] Prompt 版本管理 + A/B 灰度
- [ ] 数据生命周期 TTL 策略

**总计预估**：11-15 周（较 v1.0 的 10 周有所增加，主要是 Phase 1 因补充 Worker 和 fail-closed 逻辑而延长）。

---

## 12. 风险评估与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| **DataTrust 服务宕机** | 高 | 熔断器 fail-closed（默认）或 fail-open（可配），审计日志 + outbox 待恢复补审 |
| **审核人力不足** | 中 | 智能抽样 + 待审数据堆积告警 |
| **RAGFlow 升级冲突** | 中 | 注入点集中在 `chunk_service.py` 一处，改动范围明确 |
| **审核修正后 embedding 不匹配** | 中 | Worker 全字段同步：正文 + 重新分词 + 重算 embedding，再更新 available_int |
| **审核结果应用失败/重复** | 中 | 批次级幂等去重，写入失败重试 + 死信队列 |
| **outbox 堆积导致提交延迟** | 中 | 指数退避重放 + 超过 max_retries 告警；失败记录不删可手动重放 |
| **GraphRAG 在 pending 数据上运行** | 中 | Phase 1 保持原样（progress>=1 入队）；Phase 3 改为 DataTrust batch applied 事件驱动 |
| **mother/pending chunk mom_id 断裂** | 低 | v1.2 已修正：先建全量 mother 再分流 |
| **dry-run 污染 DataTrust** | 低 | 检测 `write_interceptor` → 跳过 HTTP 提交 |
| **Canvas Worker 被审核阻塞** | 低（Phase 3+） | 事件驱动模型，不轮询，不占并发 |
| **fail-closed 导致检索库长时间空白** | 低 | 监控 + 告警 + 超时可切换 fail-open |

---

## 13. 评审清单

落地前必须确认：

**v1.0→v1.1 已关闭项**（设计层面已回应）：

- [x] 门禁挂在默认生产执行路径 `task_executor_refactor/chunk_service.py` 上
- [x] 审核对象 schema 与真实 chunk 产出对齐（三种类型区分）
- [x] ReviewResultWorker 有明确的幂等写入 + 失败重试机制
- [x] 文本被修正后强制重算 embedding 再更新 available_int
- [x] 降级策略（fail-closed/fail-open）与产品"可信"目标一致，含审计
- [ ] GraphRAG / RAPTOR 不在半审核数据上运行（Phase 3 解决；Phase 1 已知接受——GraphRAG 按 progress>=1 入队）
- [x] Canvas 集成不占用长轮询 worker（事件驱动）
- [x] 改动文件清单与实际代码路径一致

**v1.2 新增实现级确认项**（实施前必须勾选）：

- [ ] fail-closed / 提交失败有本地 outbox，恢复后可重放补审
- [ ] batch 协议含 `tenant_id`（及 embedding 相关元数据）
- [ ] 修正入库同步分词字段 + 向量，不只改 `content_with_weight`
- [ ] 门禁不破坏 mother/TOC 创建顺序（先全量建 mother 再分流）
- [ ] 明确审核状态权威源 ≠ `available_int` 聚合
- [ ] dry-run / `write_interceptor` 下门禁无外部副作用
- [ ] 文档「已解析 vs 待审可检索」对用户可见（Phase 1 至少 progress_msg）

---

## 14. 附录

### A. 版本变更摘要

#### v1.3 → v1.3.1（基于评审修订）

| v1.3 状态 | v1.3.1 变更 |
|-----------|----------|
| §7.2 伪代码 submit 在 insert 之前（可能导致反向孤儿） | 修正：先 `_insert_main_chunks()` 落地 ES，后 `_submit_review_batch()` 通知 DataTrust，失败走 outbox |
| Step 2 关键顺序 | 更新为 `mother → gate → insert ES → submit (fail → outbox)` |
| §7.5 字段表 `important_keywords`（不存在） | 修正为 `important_kwd` + 补充 `content_sm_ltks` |
| §9.3 多余 `\`\`\`` 结尾反引号 | 移除 |
| §10.4 / §12 GraphRAG "保持现状暂不入队" vs §13 "保持原样" 矛盾 | 统一为：Phase 1 保持原样（progress>=1 入队），Phase 3 改事件驱动 |
| Step DAG 缺少 DataTrust 并行轨说明 | 新增并行轨注释 |
| §5.3 outbox schema `JSONB` 非 SQLite 合法类型 | 修正为 `TEXT` + JSON 序列化注释 |
| Step 1-3 代码行数估计过于刚硬 | 标注为软上限，实际以功能完成为准 |

#### v1.2 → v1.3（补充分步执行计划）

| v1.2 状态 | v1.3 变更 |
|-----------|----------|
| Phase 1 实施为粗粒度清单 | §11 新增 Phase 1 分步执行计划（Step 1a→1b→2→3→4），含每步文件级规格、接口定义、依赖关系、可验证标准 |
| 缺少逐层交付的验证门禁 | 每步定义独立可验证标准 + 集成验证 9 项端到端用例 |
| 不明确人工介入点 | 每步之间明确人工 `git commit` 检查点 |

#### v1.1 → v1.2（基于二次代码评审）

| v1.1 缺口 | v1.2 修正 |
|-----------|----------|
| fail-closed 孤儿 chunk 无人补审 | 新增 §5.3 outbox 机制（SQLite 本地持久化 + 退避重放） |
| mother 创建顺序错误（pending chunk 丢 mom_id） | §7.2 修正：先全量建 mother，再按策略分流设 available_int |
| Worker 输入协议缺 `tenant_id` | §9 所有协议新增 `tenant_id`；§10.1 review_batches 表加字段 |
| 修正入库只改正文、不管分词和向量 | §7.5 Worker 全字段同步清单：content + 重新分词 + 重算 embedding |
| `available_int=0` 语义过载 | 新增 §10.3 语义边界：只控制检索可见性，状态权威源 = DataTrust + outbox |
| 文档 `pending_review` 与"零字段新增"张力 | 新增 §10.4 文档状态策略：推荐方案 1（UI 聚合 DataTrust 状态） |
| §7.3 熔断描述"全部拒绝"与策略不一致 | 修正为"按 DATATRUST_FAIL_STRATEGY 执行" |
| 风险表引用不存在的 Phase 1 "类型 B" | 移除，更新为 outbox/全字段同步/mother 顺序等实际风险 |
| dry-run 无防护 | 新增 §7.4：检测 write_interceptor 跳过 HTTP 提交 |

#### v1.0 → v1.1（基于代码级评审）

| v1.0 问题 | v1.1 修正 |
|-----------|----------|
| 注入点在旧 `task_executor.py` | 注入点改为 `task_executor_refactor/chunk_service.py` |
| 假定 chunk 有 `confidence`/`extracted_fields` | 补充 Phase 0，区分三种审核对象，Phase 1 只处理纯文本 chunk |
| 审核结果应用链路缺失 | 新增 `review_result_worker.py`，完整 re-embed → available_int 更新流程 |
| Canvas "一行改动" | 改为事件驱动模型，独立排到 Phase 3+ |
| 降级 fail-open，与"可信"矛盾 | 改为 fail-closed 默认、fail-open 可配 |
| `documents` 表 ALTER 侵入 | 零字段新增，复用 `available_int` |
| 10 周乐观估计 | 重估为 11-15 周，Phase 1 从 2 周调整为 3-4 周 |
| 未提 `write_interceptor` 机制 | 门禁挂在 `insert_chunks()` 方法入口，可考虑与 interceptor 协同 |

### B. 关键环境变量

```yaml
# RAGFlow 侧
DATATRUST_URL=http://datatrust:8100
DATATRUST_FAIL_STRATEGY=fail_closed    # fail_closed | fail_open
DATATRUST_CIRCUIT_THRESHOLD=3
DATATRUST_CIRCUIT_RECOVERY=30          # 秒
REVIEW_WORKER_POLL_INTERVAL=30         # 秒
REVIEW_OUTBOX_PATH=/var/lib/ragflow/outbox.db   # [v1.2] SQLite outbox 文件路径
REVIEW_OUTBOX_MAX_RETRIES=10                      # [v1.2] 最大重试次数

# DataTrust 侧
REVIEW_DEFAULT_TIMEOUT=3600            # 审核超时秒数
GOLDEN_STANDARD_INJECTION_RATE=0.05
DATA_RETENTION_DAYS=90
```

### C. 改动文件清单

#### Phase 1

```
新增 (3个):
  rag/svr/data_trust_client.py      # HTTP 客户端 + 熔断器
  rag/svr/review_result_worker.py   # 审核结果应用 + outbox 重放 [v1.2: 扩充职责]
  rag/svr/review_outbox.py          # [v1.2 新增] SQLite outbox 队列管理

修改 (1个):
  rag/svr/task_executor_refactor/chunk_service.py  # 门禁注入（先mother后分流）

Phase 3+:
  agent/component/review_checkpoint.py  # Canvas 事件驱动审核组件
```

### D. 术语表

| 术语 | 定义 |
|------|------|
| **原始库** | DataTrust PostgreSQL，存储 LLM 原始提取结果 + 审核任务 |
| **生产库** | RAGFlow ES/Infinity，仅含审核通过（available_int=1）的 chunk |
| **审核门禁** | chunk_service 写入前的拦截逻辑 |
| **available_int** | RAGFlow chunk 现有字段，0=不可检索，1=可检索；**仅控制检索可见性，不作审核状态追踪** |
| **熔断器** | DataTrust 不可用时的自动保护机制 |
| **outbox** | [v1.2] 本地 SQLite 持久化队列，暂存熔断期无法提交的审核批次，恢复后重放 |
| **ReviewResultWorker** | 后台轮询 DataTrust + 重放 outbox 的组件，负责应用审核结果到 ES/Infinity |
| **派生数据** | GraphRAG 社区报告、RAPTOR 层级摘要等 LLM 二次加工数据 |
| **fail-closed** | DataTrust 不可用时 chunk 仍入库（`available_int=0` 不可检索），写入 outbox 待补审 |
| **fail-open** | DataTrust 不可用时直接入库（available_int=1 + 审计标记） |
| **幂等键** | `doc_id + chunk_id + data_version`，用于 outbox 重复提交去重 |

---

> **文档维护**: 此文档随 DataTrust 项目实施持续更新。设计变更请同步修订版本号，并回写 §13 评审清单。v1.3.1 修正双写顺序、字段名及 GraphRAG 策略对齐，面向人工审核。