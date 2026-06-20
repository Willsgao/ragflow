# -*- coding: utf-8 -*-
"""
LLM 表格结构修复器 — 基于语义推理修复 PDF 提取的表格结构问题

核心能力（纯语义驱动，不依赖固定位置规则）：
1. 识别合并单元格的父子层级关系（如"2024年"→4个季度）
2. 检测并合并被错误拆分的文本（如长指标名称断裂成多行）
3. 理解表头嵌套结构（年份→季度、科目→子科目等）
4. 修正行列错位（允许合理的列偏移）

输入：原始提取的2D表格数据（纯文本，无需图片）
输出：修复后的表格（扁平化，可直接导入CSV/Excel/Pandas）
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import LLM_CONFIG

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class RepairResult:
    """修复结果"""
    # 核心输出
    repaired_table: List[List[str]] = field(default_factory=list)
    """修复后的完整2D数组
    - 合并单元格已展开填充（无空值）
    - 拆分文本已合并到单行
    - 可直接用于 CSV/Excel/Pandas"""

    # 推理过程
    reasoning_summary: str = ""
    semantic_hierarchy: Dict = field(default_factory=dict)
    repairs_applied: List[Dict] = field(default_factory=list)

    # 元信息
    original_row_count: int = 0
    repaired_row_count: int = 0
    overall_confidence: float = 0.0
    uncertainty_notes: str = ""

    # 错误
    llm_error: str = ""
    llm_raw_response: str = ""
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return len(self.repaired_table) > 0 and self.llm_error == ""


# ============================================================
# Prompt 模板（语义驱动，非机械规则）
# ============================================================

REPAIR_SYSTEM_PROMPT = """你是一个具备深度语言理解、逻辑推理和模式识别能力的表格结构分析专家。

## 核心原则：语义优先，位置次要

你在分析表格时，必须遵循以下原则：

### ⚠️ 绝对禁止
- ❌ 不要假设某个内容"一定在某一列"
- ❌ 不要用固定的列索引来判断归属关系
- ❌ 不要因为数据偏移了几列就判断为错误

### ✅ 你应该做的
1. 寻找语义模式：识别重复出现的序列、层级关系、父子从属
2. 建立逻辑关联：根据内容的含义建立联系，而非物理位置
3. 容错定位：允许合理的偏移（±1~2列），重点看相对位置
4. 常识验证：用领域知识验证你的推断是否合理

---

## 你的三大核心能力

### 能力1：语义完整性检测与文本合并

**推理方法**：
```
输入观察：
  行N:   ["扣除非经常性损益后归属于本行股东的",  "",  "",  "",  "",  "",  "",  "",  ""]
  行N+1: ["净利润", "86,888", "77,446", "91,231", "79,758", ...]

推理链路：
① 语义检查：行N的首列以"...的"结尾 → 中文习惯中这通常表示还有下文
② 结构检查：行N其他列全为空 → 说明该行只有描述信息，没有对应数据
③ 上下文关联：行N+1首列是"净利润"且有完整数值数据
④ 拼接测试："...股东的" + "净利润" = 完整财务指标名 ✅
⑤ 验证确认：拼接后行N+1的数值列与其他行对齐良好
结论：✅ 这两行应合并为一行
```

### 能力2：层次结构模式识别（允许位置偏移）

**推理方法**：
```
输入观察（可能有位置偏移！）：
  行0: ["(人民币百万元)", "2024年", "", "", "", "2023年", "", "", ""]
  行1: ["", "第一季度", "第二季度", "第三季度", "第四季度", "第一季度", "第二季度", "第三季度", "第四季度"]

推理链路：
① 模式发现：扫描行1，发现 ["第一季度","第二季度","第三季度","第四季度"] 出现了 2 次
② 这是一个明显的周期性重复结构
③ 定位父级候选：在每组季度的上方或附近寻找标签
   - 第一组季度的上方 → "2024年"
   - 第二组季度的上方 → "2023年"
④ 语义验证："2024年"/"2023年"是年份标识，符合"年份→季度"的层次结构
⑤ 常识验证：财务报表通常按年份组织，每个年份下分季度展示
结论："2024年"是其下方4个季度数据的父级分组标签（无论具体在第几列）
```

### 能力3：多线索融合与置信度评估

对于每一个修复操作，综合考虑：语义完整性(30%)、结构一致性(25%)、模式匹配(20%)、领域知识(15%)、上下文连贯(10%)

---

## 输出格式要求

请严格按照以下JSON格式输出（不要包含markdown代码块标记）：

{
  "reasoning_summary": "用3-5句话总结你的整体推理过程和发现的主要问题",

  "semantic_hierarchy": {
    "table_type": "表格类型（如：财务报表_季度对比）",
    "header_levels": [
      {
        "level": 0,
        "description": "最顶层（如：年份分组）",
        "labels": ["2024年", "2023年"],
        "children_pattern": "第一季度→第二季度→第三季度→第四季度"
      }
    ]
  },

  "repairs_applied": [
    {
      "type": "text_merge | hierarchy_fill | row_delete",
      "what_you_did": "做了什么操作（人话描述）",
      "reasoning_chain": [
        "第一步观察到什么",
        "第二步推断出什么",
        "第三步验证了什么",
        "最终结论"
      ],
      "evidence": [
        "证据1（语义层面）",
        "证据2（结构层面）"
      ],
      "confidence": 0.95
    }
  ],

  "repaired_table": [
    // 修复后的完整表格
    // 要求：
    // 1. 合并单元格必须展开填充到所有子列（不能有空值！）
    //    例如："2024年"应出现在 col 1-4 每一列（而非只有 col 1 有值）
    // 2. 拆分文本必须合并到单行并删除多余空行
    // 3. 每行保持相同列数，可直接用于CSV/Excel导入
  ],

  "metadata": {
    "original_row_count": 7,
    "repaired_row_count": 6,
    "total_repairs": {"text_merges": 1, "hierarchy_fills": 2, "row_deletions": 1},
    "overall_confidence": 0.93,
    "uncertainty_notes": ""
  }
}
"""


# ============================================================
# 工具函数
# ============================================================

def _format_table_for_llm(table_data: List[List[str]]) -> str:
    """将2D表格格式化为LLM可读的文本"""
    if not table_data:
        return "(空表格)"

    lines = []
    for i, row in enumerate(table_data):
        row_str = " | ".join(
            f'"{cell}"' if cell else '""' for cell in row
        )
        lines.append(f"[{i:3d}] {row_str}")

    return "\n".join(lines)


def _build_repair_user_prompt(
    raw_table_data: List[List[str]],
    context: str = ""
) -> str:
    """构建给LLM的用户prompt"""
    table_str = _format_table_for_llm(raw_table_data)

    prompt = f"""请修复以下从PDF提取的表格数据。

【额外上下文信息】
{context if context else "(无特殊上下文)"}

【原始表格数据】（注意：可能存在以下结构错误）
- 合并单元格被展开为多个重复/空单元格
- 长文本被错误拆分成多行
- 行列对齐可能有偏移

{table_str}

【任务】
请从语义层面分析上述表格：
1. 识别表头层级关系（如年份 > 季度等父子关系）
2. 检测被错误拆分的长文本并合并
3. 将合并单元格展开填充（不要留空值！）
4. 输出修复后的正确表格结构

只需输出JSON格式的结果。"""

    return prompt


def _parse_llm_json_response(content: str) -> dict:
    """解析LLM返回的JSON响应"""
    # 尝试1：直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试2：提取 ```json ... ``` 代码块
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试3：提取第一个 {...} 对象
    brace_match = re.search(r'\{[\s\S]*\}', content)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return {"error": "无法解析JSON响应", "raw_preview": content[:500]}


def _convert_to_result(parsed: dict) -> RepairResult:
    """将LLM返回的JSON转换为RepairResult"""
    result = RepairResult()

    result.reasoning_summary = parsed.get("reasoning_summary", "")
    result.semantic_hierarchy = parsed.get("semantic_hierarchy", {})
    result.repairs_applied = parsed.get("repairs_applied", [])

    # 修复后的表格
    repaired = parsed.get("repaired_table", [])
    if repaired and isinstance(repaired, list):
        result.repaired_table = [
            [str(cell) if cell is not None else "" for cell in row]
            for row in repaired
        ]

    # 元信息
    meta = parsed.get("metadata", {})
    result.original_row_count = meta.get("original_row_count", 0)
    result.repaired_row_count = meta.get("repaired_row_count", len(result.repaired_table))
    result.overall_confidence = meta.get("overall_confidence", 0.0)
    result.uncertainty_notes = meta.get("uncertainty_notes", "")

    return result


# ============================================================
# 代码层面确定性修复
# ============================================================
# LLM 擅长语义分析（识别层级、描述修复），但在生成 repaired_table
# 时经常忘记实际执行修复操作。以下函数根据 LLM 的分析结果，
# 用确定性代码逻辑执行实际的表格修复，确保修复操作真实生效。


def _is_effectively_empty(cell: str) -> bool:
    """判断单元格是否为"有效空"

    PDF 提取常产生各种非标准空白字符，典型包括：
    - \\u00a0  不间断空格 (NBSP，合并单元格展平后常见)
    - \\u200b  零宽空格
    - \\u200c-\\u200d  零宽连字/断字
    - \\ufeff  BOM / 零宽不换行空格
    - \\u3000  全角空格（中文 PDF 常见）
    - \\t, \
, 连续空格

    这些字符在视觉上为空但 .strip() 无法清除，
    或被 repr 隐藏但 len>0，导致错误判断为 "有内容"。
    """
    if cell is None:
        return True
    if not isinstance(cell, str):
        return False
    # 移除全部 Unicode 空白类字符后检查是否为空
    cleaned = re.sub(r'[\s\u00a0\u200b\u200c\u200d\ufeff\u3000]+', '', cell)
    return len(cleaned) == 0


def _apply_code_level_repairs(
    original_table: List[List[str]],
    repairs_applied: List[Dict],
) -> List[List[str]]:
    """
    根据 LLM 识别的修复类型，用确定性代码执行实际修复。

    这解决了 LLM 的常见缺陷：在 repairs_applied 中正确描述了
    修复操作，但在 repaired_table 中却忘了实际执行。
    """
    if not original_table:
        logger.info("_apply_code_level_repairs: empty table, skip")
        return []

    table = [row[:] for row in original_table]  # 深拷贝

    repair_types = set(r.get("type", "") for r in repairs_applied)
    logger.info(
        f"_apply_code_level_repairs: {len(repairs_applied)} repairs, "
        f"types={repair_types}, orig_rows={len(original_table)}, "
        f"orig_cols={len(original_table[0]) if original_table else 0}"
    )

    if "hierarchy_fill" in repair_types:
        logger.info("→ running _code_hierarchy_fill...")
        table = _code_hierarchy_fill(table)

    if "text_merge" in repair_types:
        logger.info("→ running _code_text_merge...")
        table = _code_text_merge(table)

    if "row_delete" in repair_types:
        logger.info("→ running _code_row_delete...")
        table = _code_row_delete(table)

    # 对比变化：检查首行是否有差异
    if len(original_table) == len(table):
        for ri in range(min(3, len(table))):
            orig_row = original_table[ri] if ri < len(original_table) else []
            new_row = table[ri] if ri < len(table) else []
            diffs = [
                j for j in range(min(len(orig_row), len(new_row)))
                if orig_row[j] != new_row[j]
            ]
            if diffs:
                logger.info(
                    f"  row[{ri}] changed cols: {diffs[:8]}, "
                    f"orig={orig_row[:6]}, new={new_row[:6]}"
                )

    logger.info(
        f"_apply_code_level_repairs done: "
        f"{len(original_table)}→{len(table)} rows"
    )
    return table


def _code_hierarchy_fill(table: List[List[str]]) -> List[List[str]]:
    """
    将表头行中的父级标签向右展开填充空单元格。

    例如：
      行0: ["", "2024年", "", "", "2023年", "", ""]
    → 修复后:
      行0: ["", "2024年", "2024年", "2024年", "2023年", "2023年", "2023年"]

    逻辑：
    1. 检测表头行（前 N 行，其中大部分列包含标签型文本）
    2. 对于每个标签单元格，向右填充直到遇到下一个非空标签
    3. 首列如果为空则保持为空（通常是合并的转角单元格）
    """
    if not table:
        return table

    max_header_rows = min(4, len(table))

    for row_idx in range(max_header_rows):
        row = table[row_idx]
        last_label = ""
        fills_applied = 0

        for col_idx in range(len(row)):
            cell = row[col_idx]
            if not _is_effectively_empty(cell):
                last_label = str(cell).strip()
            elif last_label and col_idx > 0:
                # 前面有标签且当前列为有效空 → 用标签填充
                old_val = repr(row[col_idx])
                row[col_idx] = last_label
                fills_applied += 1
                logger.debug(
                    f"  hierarchy_fill row={row_idx} col={col_idx}: "
                    f"{old_val} → '{last_label}'"
                )

        if fills_applied:
            logger.info(
                f"hierarchy_fill applied: row={row_idx}, "
                f"fills={fills_applied}, row_snapshot={row[:8]}"
            )

    return table


def _code_text_merge(table: List[List[str]]) -> List[List[str]]:
    """
    检测并合并被错误拆分的文本行。

    识别模式：
    - 某行只有 col[0] 有文本，其余列全为空
    - col[0] 文本以中文连接词结尾（"的"/"和"/"及"等），暗示还有下文
    - 下一行首列内容可以与之拼接成完整短语

    处理：将当前行的 col[0] 拼接到下一行的 col[0] 前，删除当前行。
    """
    if len(table) < 2:
        return table

    # 中文文本拆分提示词：以这些字符结尾通常表示内容未完
    CONTINUATION_MARKERS = ("的", "和", "及", "与", "、", "（", "(")

    result = []
    skip_next = False
    i = 0

    while i < len(table):
        if skip_next:
            skip_next = False
            i += 1
            continue

        if i >= len(table) - 1:
            result.append(list(table[i]))
            break

        row = table[i]
        next_row = table[i + 1]

        col0 = (row[0] or "").strip()
        # 检查 col[0] 之外的其他列是否全为空
        other_cols_nonempty = any(
            (c or "").strip() for j, c in enumerate(row) if j > 0
        )

        if col0 and not other_cols_nonempty and len(row) > 1:
            # 判断是否为拆分文本（以中文连接词结尾）
            is_split_text = col0.endswith(CONTINUATION_MARKERS)

            if is_split_text:
                next_col0 = (next_row[0] or "").strip()
                merged = next_row[:]
                merged[0] = col0 + next_col0
                result.append(merged)
                skip_next = True
                i += 1
                continue

        result.append(list(row))
        i += 1

    return result


def _code_row_delete(table: List[List[str]]) -> List[List[str]]:
    """
    删除全空行（所有单元格均为空或纯空白）。
    """
    return [
        row for row in table
        if any((cell or "").strip() for cell in row)
    ]


# ============================================================
# 主函数：调用LLM修复表格
# ============================================================

def repair_table_with_llm(
    raw_table_data: List[List[Any]],
    context: str = "",
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    model: Optional[str] = None,
    max_retries: int = 2,
) -> RepairResult:
    """
    用LLM修复表格结构（语义推理驱动）

    Args:
        raw_table_data: 原始2D表格数据
        context: 额外上下文（如"银行季度财报"）
        api_key: API Key
        endpoint: API端点
        model: 模型名称
        max_retries: 最大重试次数

    Returns:
        RepairResult: 修复后的表格 + 推理过程
    """
    result = RepairResult()

    # 预处理：统一转字符串
    processed_data = []
    for row in raw_table_data:
        processed_row = []
        for cell in row:
            if cell is None:
                processed_row.append("")
            elif isinstance(cell, (int, float)):
                processed_row.append(str(cell))
            else:
                processed_row.append(str(cell).strip())
        processed_data.append(processed_row)

    result.original_row_count = len(processed_data)

    # 加载配置
    from codes.pdf_extractor.utils import load_config
    config = load_config()
    api_key = api_key or config.get("deepseek_api_key", "")
    endpoint = endpoint or config.get("deepseek_endpoint", "api.deepseek.com")
    model = model or config.get("deepseek_model", "deepseek-chat")

    if not api_key:
        result.llm_error = "未配置DeepSeek API Key"
        logger.error("repair_table_with_llm: No API key")
        return result

    # 构建请求
    api_url = f"https://{endpoint}/v1/chat/completions"
    user_prompt = _build_repair_user_prompt(processed_data, context)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": LLM_CONFIG["max_output_tokens"],
        "temperature": LLM_CONFIG["temperature"],
    }

    # 调用LLM（含重试）
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"LLM table repair attempt {attempt + 1}/{max_retries + 1}...")

            resp = requests.post(
                api_url, headers=headers, json=payload,
                timeout=LLM_CONFIG["timeout_sec"]
            )
            resp.raise_for_status()
            resp_json = resp.json()

            result.usage = resp_json.get("usage", {})

            if "choices" in resp_json and len(resp_json["choices"]) > 0:
                content = resp_json["choices"][0]["message"]["content"]
                result.llm_raw_response = content

                parsed = _parse_llm_json_response(content)

                if "error" in parsed:
                    result.llm_error = f"JSON解析失败: {parsed.get('error', '')}"
                    if attempt == max_retries:
                        logger.warning("LLM JSON parse failed, returning original")
                        result.repaired_table = processed_data
                else:
                    result = _convert_to_result(parsed)
                    # 保留usage和raw_response
                    result.usage = resp_json.get("usage", {})
                    result.llm_raw_response = content

                    # 代码层面确定性修复：
                    # LLM 常年在 repairs_applied 中正确描述修复操作，
                    # 但在 repaired_table 中忘记实际执行。这里根据
                    # LLM 分析的修复类型，用确定性代码重新执行修复。
                    if result.repairs_applied:
                        logger.info(
                            f"LLM identified {len(result.repairs_applied)} repairs, "
                            f"running code-level fixes on processed_data "
                            f"({len(processed_data)} rows)..."
                        )
                        code_table = _apply_code_level_repairs(
                            processed_data, result.repairs_applied
                        )
                        if code_table != processed_data:
                            result.repaired_table = code_table
                            result.repaired_row_count = len(code_table)
                            logger.info(
                                f"Code-level repair applied: "
                                f"{len(result.repairs_applied)} repairs, "
                                f"{len(processed_data)}→{len(code_table)} rows"
                            )
                        else:
                            logger.warning(
                                f"Code-level repair produced NO change! "
                                f"repair_types={set(r.get('type','?') for r in result.repairs_applied)}, "
                                f"row0_sample={processed_data[0][:6] if processed_data else 'N/A'}"
                            )

                    logger.info(
                        f"Table repair done: {result.original_row_count}→{result.repaired_row_count} rows, "
                        f"confidence={result.overall_confidence:.2f}"
                    )
            else:
                result.llm_error = "LLM返回空结果"

        except requests.exceptions.Timeout:
            result.llm_error = f"请求超时（{LLM_CONFIG['timeout_sec']}秒）"
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue

        except requests.exceptions.RequestException as e:
            result.llm_error = f"API请求失败: {e}"
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue

        except Exception as e:
            result.llm_error = f"未知错误: {e}"
            logger.exception(f"LLM repair error: {e}")
            break

        break

    return result


# ============================================================
# LLM 异常确认接口（后期使用，当前阶段仅预留）
# ============================================================

CONFIRM_ANOMALIES_SYSTEM_PROMPT = """你是一个表格结构审核专家。用户会提供：
1. 一个经过规则引擎修复的表格
2. 规则引擎在修复过程中检测到的异常列表

请逐项审核每个异常，判断规则引擎的修复操作是否正确，或是否需要修正。

对于每个异常，输出：
- approved: true/false — 规则引擎的修复是否正确
- corrected_action: 如果错误，应该怎么修正（描述具体操作）
- reasoning: 1-2句话说明判断理由

输出 JSON 格式：
{
  "overall_assessment": "总体判断（1-2句话）",
  "confirmed_correct": true/false,
  "anomaly_reviews": [
    {
      "anomaly_index": 0,
      "type": "anchor_shift",
      "approved": true,
      "corrected_action": "",
      "reasoning": "..."
    }
  ],
  "final_repair_suggestions": ["建议1", "建议2"]
}
"""


def confirm_anomalies_with_llm(
    repaired_table: List[List[str]],
    rule_repair_info: dict,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """用 LLM 确认规则修复中检测到的异常

    两阶段修复流程的第二阶段入口：
    1. 先调用 repair_table_rules() 获得修复结果 + 异常列表
    2. 如果有中高严重度异常，调用本函数让 LLM 确认

    Args:
        repaired_table: 规则引擎修复后的表格
        rule_repair_info: repair_table_rules() 返回的 info 字典
        api_key: LLM API Key
        endpoint: LLM API 端点
        model: LLM 模型名称

    Returns:
        {
            "overall_assessment": str,
            "confirmed_correct": bool,
            "anomaly_reviews": [...],
            "final_repair_suggestions": [...],
        }
        如果调用失败，返回 {"error": "..."}
    """
    from codes.pdf_extractor.utils import load_config
    from .rule_based_repair import prepare_anomalies_for_llm

    anomalies = rule_repair_info.get('anomalies', [])
    if not anomalies:
        return {
            "overall_assessment": "无异常需要确认",
            "confirmed_correct": True,
            "anomaly_reviews": [],
            "final_repair_suggestions": [],
        }

    # 加载配置
    config = load_config()
    api_key = api_key or config.get("deepseek_api_key", "")
    endpoint = endpoint or config.get("deepseek_endpoint", "api.deepseek.com")
    model = model or config.get("deepseek_model", "deepseek-chat")

    if not api_key:
        return {"error": "未配置 LLM API Key，无法进行异常确认"}

    # 构建 prompt
    anomaly_text = prepare_anomalies_for_llm(rule_repair_info)
    table_text = _format_table_for_llm(repaired_table)

    # 收集 multi_table_merged 异常的原始上下文
    context_blocks = []
    for a in anomalies:
        if a.get('type') == 'multi_table_merged':
            parts = []
            if a['details'].get('context_description'):
                parts.append(
                    "【表格上方描述文本（修复时已移除）】\n" +
                    "\n".join(a['details']['context_description'])
                )
            if a['details'].get('separator_rows'):
                parts.append(
                    "【疑似分隔行（两个表格之间的分隔）】\n" +
                    "\n".join(a['details']['separator_rows'])
                )
            if a['details'].get('orphan_preview'):
                parts.append(
                    "【被丢弃的孤儿数据预览（疑似第二张表格）】\n" +
                    "\n".join(a['details']['orphan_preview'])
                )
            if parts:
                context_blocks.append("\n\n".join(parts))

    context_section = ""
    if context_blocks:
        context_section = (
            "【原始表格上下文（用于判断多表合并，修复后的表格只包含第一张表）】\n"
            + "\n\n---\n\n".join(context_blocks)
            + "\n\n"
        )

    user_prompt = f"""请审核以下规则引擎修复表格时检测到的异常：

{context_section}【异常列表】
{anomaly_text}

【修复后的表格】
{table_text}

【任务】
逐项审核每个异常，判断规则引擎的修复是否正确。
只输出 JSON 格式结果。"""

    api_url = f"https://{endpoint}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CONFIRM_ANOMALIES_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.2,  # 低温度，确保一致判断
    }

    try:
        resp = requests.post(
            api_url, headers=headers, json=payload,
            timeout=120
        )
        resp.raise_for_status()
        resp_json = resp.json()

        if "choices" in resp_json and len(resp_json["choices"]) > 0:
            content = resp_json["choices"][0]["message"]["content"]
            parsed = _parse_llm_json_response(content)

            if "error" in parsed:
                return {
                    "error": f"LLM 返回解析失败: {parsed.get('error', '')}",
                    "raw_response": content[:500],
                }
            return parsed

        return {"error": "LLM 返回空结果"}

    except Exception as e:
        logger.exception(f"LLM anomaly confirmation error: {e}")
        return {"error": f"LLM 调用失败: {e}"}


# ============================================================
# 辅助：生成可读报告
# ============================================================

def generate_repair_report(result: RepairResult) -> str:
    """生成人类可读的修复报告"""
    if not result.success:
        return f"❌ 表格修复失败: {result.llm_error}"

    lines = []
    lines.append("=" * 60)
    lines.append("📊 LLM 表格结构修复报告")
    lines.append("=" * 60)
    lines.append("")

    if result.reasoning_summary:
        lines.append("🧠 语义分析:")
        lines.append(f"   {result.reasoning_summary}")
        lines.append("")

    if result.semantic_hierarchy:
        lines.append("📐 识别到的表头层级:")
        h = result.semantic_hierarchy
        lines.append(f"   表格类型: {h.get('table_type', '未知')}")
        for lv in h.get("header_levels", []):
            lines.append(f"   Level {lv.get('level', 0)} ({lv.get('description', '')}): "
                         f"{lv.get('labels', [])}")
        lines.append("")

    if result.repairs_applied:
        lines.append(f"🔧 修复操作 ({len(result.repairs_applied)} 处):")
        for i, r in enumerate(result.repairs_applied, 1):
            lines.append(f"   {i}. [{r.get('type', '?')}] {r.get('what_you_did', '')}")
            lines.append(f"      置信度: {r.get('confidence', 0):.0%}")
        lines.append("")

    lines.append("📈 修复统计:")
    lines.append(f"   原始 {result.original_row_count} 行 → 修复后 {result.repaired_row_count} 行")
    lines.append(f"   整体置信度: {result.overall_confidence:.1%}")

    if result.uncertainty_notes:
        lines.append(f"   ⚠ 不确定: {result.uncertainty_notes}")

    if result.usage:
        tokens = result.usage.get('total_tokens', '?')
        lines.append(f"   Token用量: {tokens}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
