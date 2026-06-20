# -*- coding: utf-8 -*-
"""
LLM Checker — 调用 DeepSeek API 对真表格页进行深度验证

将 liteparse full_text（空间真值） + pdf2docx table data（结构化结果）
一起交给 LLM，让 LLM 判断 5 个维度的问题。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, List, Optional, Any

import requests

from .config import LLM_CONFIG
from .models import LLMVerifyResult

logger = logging.getLogger(__name__)


# ============================================================
# Prompt 模板
# ============================================================

SYSTEM_PROMPT = """你是一个 PDF 表格数据验证专家。你的任务是：对比"PDF 原始版式文本"（来自 liteparse 空间解析）和"结构化表格数据"（来自 pdf2docx 解析器），判断表格数据是否存在以下 5 类问题。

请严格按 JSON 格式回复，不要包含任何额外说明文字。

{
  "header_correct": true,
  "header_issues": [],
  "has_duplicate_header": false,
  "has_duplicate_data": false,
  "duplicate_details": [],
  "has_misalignment": false,
  "misalignment_details": [],
  "has_footer_text": false,
  "footer_from_row": -1,
  "footer_details": [],
  "needs_merge_prev": false,
  "needs_merge_next": false,
  "merge_details": [],
  "overall_assessment": "简述表格质量和发现的主要问题"
}

检查规则（请严格按照以下逻辑判断，不要臆测）：

1. **header_correct**: 表格的第一行是否是表头？（表头通常包含科目名称、年份、单位等非纯数值文本）
   - 如果第一行看起来是数据行（包含很多数值），设为 false 并在 header_issues 中说明
   - 如果表头不止一行，请判断所有表头行是否完整

2. **has_duplicate_header / has_duplicate_data**: 是否有重复？
   - 重复表头：表头行在数据行中再次出现（如"资产"出现在中间行）
   - 重复数据：相邻或相近行完全一致（真正的重复行）

3. **has_misalignment**: 数据是否有列偏移？
   - 检查数值列是否出现在正确的列位置
   - 例如原本应在第3列的数值跑到了第2列

4. **has_footer_text**: 表格底部是否混入了非表格文本？
   - 典型的混入文本：页面注释、说明文字、页码、"单位：万元"、数据来源说明等
   - footer_from_row 指定从哪一行（0-based，含表头行）开始是混入文本
   - 如果没有混入，footer_from_row 设为 -1

5. **needs_merge_prev / needs_merge_next**: 表格是否完整？
   - needs_merge_prev: 表格前面缺少表头/开头 → 可能是前一页表格的续行
   - needs_merge_next: 表格末尾数据不完整/被截断 → 需要下一页续行
   - 注意：除非有明显证据（如行被截断、数据明显不完整），否则请不要轻易判定需要拼接"""


def _build_user_prompt(
    page_num: int,
    liteparse_full_text: str,
    table_data: List[List[str]],
    table_regions: Optional[List[dict]] = None,
) -> str:
    """构建给 LLM 的用户 prompt"""
    # 格式化表格数据为可读文本
    table_str = _format_table_for_llm(table_data)

    # 截断过长的 full_text
    max_chars = LLM_CONFIG["max_full_text_chars"]
    full_text = liteparse_full_text
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n... (内容过长，已截断)"

    # 表格区域信息
    region_str = ""
    if table_regions:
        regions_desc = []
        for i, tr in enumerate(table_regions):
            regions_desc.append(
                f"  区域{i}: ({tr.get('x0',0):.0f},{tr.get('y0',0):.0f})"
                f"-({tr.get('x1',0):.0f},{tr.get('y1',0):.0f})"
                f" 置信度={tr.get('confidence',0):.2f}"
            )
        region_str = "表格区域信息:\n" + "\n".join(regions_desc) + "\n"

    prompt = f"""请验证第 {page_num} 页的表格数据。

【PDF 原始版式文本】（来自 liteparse 空间解析，保留了空间布局）

{full_text}

{region_str}
【结构化表格数据】（来自 pdf2docx 解析器）

{table_str}

请根据以上两份数据，判断表格数据是否存在 5 类问题（表头、重复、错位、底部混入文本、拼接需求）。
只需输出 JSON，不要包含其他文字。"""

    return prompt


def _format_table_for_llm(data: List[List[str]]) -> str:
    """将 2D 表格格式化为 LLM 可读的文本"""
    if not data:
        return "(空表格)"

    max_rows = LLM_CONFIG["max_table_rows_send"]
    truncated = len(data) > max_rows

    rows_to_show = data[:max_rows // 2] + data[-(max_rows // 2):] if truncated else data

    lines = []
    # 用 tab 分隔
    for i, row in enumerate(rows_to_show):
        actual_idx = i
        if truncated and i >= max_rows // 2:
            actual_idx = len(data) - (max_rows - i)
        row_str = "\t".join(str(cell) if cell is not None else "" for cell in row)
        lines.append(f"  [行{actual_idx}] {row_str}")

    if truncated:
        lines.insert(max_rows // 2, f"  ... (中间省略 {len(data) - max_rows} 行) ...")

    return "\n".join(lines)


def _parse_llm_response(content: str) -> dict:
    """解析 LLM 的 JSON 回复"""
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试匹配第一个 { ... } 对象
    brace_match = re.search(r'\{.*\}', content, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return {"error": f"无法解析 LLM 回复为 JSON", "raw": content[:500]}


def verify_table_with_llm(
    page_num: int,
    liteparse_full_text: str,
    table_data: List[List[str]],
    table_regions: Optional[List[dict]] = None,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMVerifyResult:
    """用 LLM 验证单张表格

    Args:
        page_num: 页码
        liteparse_full_text: liteparse 解析的全文文本
        table_data: pdf2docx 解析出的 2D 表格数组
        table_regions: liteparse 检测到的表格区域（可选）
        api_key: DeepSeek API Key
        endpoint: API 端点
        model: 模型名称

    Returns:
        LLMVerifyResult
    """
    # API 配置优先级：参数 > config
    from codes.pdf_extractor.utils import load_config

    config = load_config()
    api_key = api_key or config.get("deepseek_api_key", "")
    endpoint = endpoint or config.get("deepseek_endpoint", "api.deepseek.com")
    model = model or config.get("deepseek_model", "deepseek-chat")

    if not api_key:
        return LLMVerifyResult(
            page=page_num,
            llm_error="未配置 DeepSeek API Key，请在「配置」Tab 中设置",
        )

    api_url = f"https://{endpoint}/v1/chat/completions"

    user_prompt = _build_user_prompt(page_num, liteparse_full_text, table_data, table_regions)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": LLM_CONFIG["max_output_tokens"],
        "temperature": LLM_CONFIG["temperature"],
    }

    result = LLMVerifyResult(page=page_num)

    try:
        resp = requests.post(api_url, headers=headers, json=payload,
                             timeout=LLM_CONFIG["timeout_sec"])
        resp.raise_for_status()
        resp_json = resp.json()

        # 提取 usage
        usage = resp_json.get("usage", {})

        if "choices" in resp_json and len(resp_json["choices"]) > 0:
            content = resp_json["choices"][0]["message"]["content"]
            result.llm_raw_response = content
            parsed = _parse_llm_response(content)

            if "error" in parsed:
                result.llm_error = f"JSON解析失败: {parsed.get('error', '')}"
                logger.warning(f"Page {page_num} LLM response parse error: {parsed['error']}")
            else:
                # 填充结果
                result.header_correct = parsed.get("header_correct", True)
                result.header_issues = parsed.get("header_issues", [])
                result.has_duplicate_header = parsed.get("has_duplicate_header", False)
                result.has_duplicate_data = parsed.get("has_duplicate_data", False)
                result.duplicate_details = parsed.get("duplicate_details", [])
                result.has_misalignment = parsed.get("has_misalignment", False)
                result.misalignment_details = parsed.get("misalignment_details", [])
                result.has_footer_text = parsed.get("has_footer_text", False)
                result.footer_from_row = parsed.get("footer_from_row", -1)
                result.footer_details = parsed.get("footer_details", [])
                result.needs_merge_prev = parsed.get("needs_merge_prev", False)
                result.needs_merge_next = parsed.get("needs_merge_next", False)
                result.merge_details = parsed.get("merge_details", [])

                logger.info(
                    f"Page {page_num}: header_ok={result.header_correct}, "
                    f"dup_header={result.has_duplicate_header}, "
                    f"dup_data={result.has_duplicate_data}, "
                    f"misalign={result.has_misalignment}, "
                    f"footer={result.has_footer_text}, "
                    f"merge_prev={result.needs_merge_prev}, "
                    f"merge_next={result.needs_merge_next}"
                )

            result.usage = usage
        else:
            result.llm_error = "LLM 返回空结果"

    except requests.exceptions.Timeout:
        result.llm_error = f"请求超时（{LLM_CONFIG['timeout_sec']}秒）"
        logger.warning(f"Page {page_num}: LLM request timeout")
    except requests.exceptions.RequestException as e:
        result.llm_error = f"API 请求失败: {str(e)}"
        logger.error(f"Page {page_num}: LLM request error: {e}")
    except Exception as e:
        result.llm_error = f"未知错误: {str(e)}"
        logger.exception(f"Page {page_num}: LLM unknown error")

    return result
