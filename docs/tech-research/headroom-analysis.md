# Headroom 项目调研 & 融合分析

> 日期：2026-06-22  
> 项目地址：https://github.com/headroomlabs-ai/headroom

---

## 1. 项目简介

**Headroom** 是一个 AI Agent 上下文压缩层（Context Compression Layer），在内容到达 LLM 之前进行智能压缩。

| 维度 | 说明 |
|---|---|
| **定位** | 外部代理/中间件，拦截 LLM 输入输出 |
| **语言** | Rust |
| **集成方式** | wrap 命令 / proxy 模式 / MCP Server |
| **核心数据** | 减少 60-95% token，回答质量零损失 |
| **压缩对象** | 工具输出、日志、RAG chunks、文件内容、对话历史 |
| **技术** | 6 种自适应压缩算法 + 可逆缓存（CCR） |
| **热度** | 12k+ GitHub stars，支持 Claude Code / Codex / Cursor 等 |
| **许可证** | Apache 2.0 |

---

## 2. 与我现有项目的融合分析

### 2.1 harnessloop（Agent 编排框架）

| 维度 | 分析 |
|---|---|
| **相似性** | 无。Headroom 是中间件，harnessloop 是框架库 |
| **冲突点** | harnessloop 核心卖点是"零外部依赖"，Headroom 是 Rust 二进制 |
| **融合价值** | **低**。用户可以在 `llm_call` 回调中自行压缩，不需要侵入框架 |
| **结论** | ❌ 不融入。保持 harnessloop 精简单一 |

**替代方案**：如果需要，v0.3.0 可作为 Loop Engineering 可选模块 `ContextCompressor`，用纯 Python 实现 JSON 裁剪 + 关键字段过滤 + 摘要，保持零依赖。

---

### 2.2 RAGFlow（RAG + Agent 平台）

| 维度 | 分析 |
|---|---|
| **相似性** | 高。RAGFlow 的上下文管理正是 Headroom 要解决的问题 |
| **现状问题** | `message_fit_in()` 粗暴 token 截断，无语义保留 |
| **融合价值** | **高**。多处上下文膨胀需要压缩 |
| **结论** | ✅ 建议引入。v0.27 作为 Context Compression 功能 |

**攻击面（4 个关键文件）**：

| 位置 | 文件 | 当前做法 | 压缩方案 |
|---|---|---|---|
| message_fit_in | `rag/prompts/generator.py` | token 硬截断 | 语义压缩，保留关键信息 |
| kb_prompt | `rag/prompts/generator.py` | token 截断丢弃 chunks | 压缩后容纳更多检索结果 |
| Agent _fit_messages | `agent/component/agent_with_tools.py` | 截断 history | 压缩 tool call 历史为摘要 |
| LLM tool call 循环 | `rag/llm/chat_model.py` | 历史无压缩累积 | 压缩后保持窗口不爆 |

**实施方案**：参考 Headroom 思路，用 Python/Go 在 RAGFlow 内实现轻量版：
- JSON 输出 → 只保留关键字段 + schema 摘要
- RAG chunks → 合并相似段落 + 去重
- 对话历史 → 压缩为摘要 + 保留最近 2 轮完整
- 工具返回 → 提取结果摘要 + 截断无关数据

---

### 2.3 BankDataViz（银行数据可视化）

| 维度 | 分析 |
|---|---|
| **关系** | 间接。BankDataViz 使用 harnessloop，LLM 调用量小 |
| **融合价值** | **低**。当前 token 不是瓶颈 |
| **结论** | ❌ 暂不需要 |

---

## 3. 汇总建议

| 优先级 | 目标项目 | 行动 |
|---|---|---|
| **P0 当前** | 无 | Headroom 先观望，不急于集成 |
| **P1 v0.27** | RAGFlow | 实现内建 Context Compression 模块 |
| **P2 v0.3.0** | harnessloop | 可选纯 Python `ContextCompressor` |
| **暂缓** | BankDataViz | token 不是瓶颈，暂不需要 |

---

## 4. 参考资源

- [Headroom GitHub](https://github.com/headroomlabs-ai/headroom)
- [Headroom 官网](https://headroomlabs.ai/)
- [掘金：Headroom 完全指南](https://juejin.cn/post/7647372504860311594)
