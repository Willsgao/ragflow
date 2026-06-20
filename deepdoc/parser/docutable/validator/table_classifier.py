# -*- coding: utf-8 -*-
"""
Table Classifier — 规则判定真表格 vs 假表格

基于 4 条件的 AND 组合：
① 至少一列纯数值（排除首行表头，数值占比 > 70%）
② 不是目录页（排除重复值列 + 目录关键词）
③ 数据行 ≥ 3（排除首行表头）
④ 列数 ≥ 2
"""

from __future__ import annotations

from typing import Dict, List, Any

from .config import CLASSIFIER_CONFIG, TOC_KEYWORDS
from .models import ClassifyResult


def _is_numeric_value(val: str) -> bool:
    """判断一个单元格是否为数值（含千分位、百分号、括号负数）"""
    if val is None:
        return False
    s = str(val).strip().rstrip('%').replace(',', '').replace(' ', '')
    # 括号负数: (123) → -123
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _column_numeric_ratio(col_values: List[str]) -> float:
    """计算一列（排除首行表头）的数值占比"""
    if len(col_values) <= 1:
        return 0.0
    data_values = col_values[1:]  # 排除首行表头
    non_empty = [v for v in data_values if str(v).strip()]
    if not non_empty:
        return 0.0
    numeric_count = sum(1 for v in non_empty if _is_numeric_value(v))
    return numeric_count / len(non_empty)


def _column_duplicate_ratio(col_values: List[str]) -> float:
    """计算一列中值完全相同的比例（用于检测目录页）"""
    if not col_values:
        return 0.0
    from collections import Counter
    counter = Counter(str(v).strip() for v in col_values)
    if not counter:
        return 0.0
    most_common_count = counter.most_common(1)[0][1]
    return most_common_count / len(col_values)


def _contains_toc_keyword(data: List[List[str]]) -> bool:
    """检查表格中是否明显包含目录页关键词"""
    for row in data:
        for cell in row:
            cell_str = str(cell).strip() if cell else ""
            for kw in TOC_KEYWORDS:
                if kw.lower() in cell_str.lower():
                    # 某列含该关键词的比例 ≥ 50% → 目录页
                    col_idx = row.index(cell) if cell in row else -1
                    if col_idx >= 0:
                        col_vals = []
                        for r in data:
                            if col_idx < len(r):
                                col_vals.append(str(r[col_idx]).strip())
                        match_count = sum(
                            1 for v in col_vals
                            if kw.lower() in v.lower()
                        )
                        if match_count / max(len(col_vals), 1) >= 0.5:
                            return True
    return False


def classify_page(data: List[List[str]], page_num: int) -> ClassifyResult:
    """对单页 pdf2docx 解析出的 2D 表格数据进行分类

    Args:
        data: 2D 表格数据 [[cell, cell, ...], ...]
        page_num: 页码（仅用于结果标识）

    Returns:
        ClassifyResult
    """
    checks: Dict[str, Any] = {}
    reason_parts: List[str] = []

    if not data or not isinstance(data, list):
        return ClassifyResult(
            page=page_num, is_real_table=False, confidence=0.9,
            reason="无数据或数据格式异常",
            checks={"error": "empty_or_invalid_data"},
        )

    # 规范化：确保所有行的长度一致
    max_cols = max(len(r) for r in data) if data else 0

    # ---- ④ 列数 ≥ 2 ----
    checks["col_count"] = max_cols
    if max_cols < CLASSIFIER_CONFIG["min_columns"]:
        return ClassifyResult(
            page=page_num, is_real_table=False, confidence=0.95,
            reason=f"列数不足（{max_cols} < 2），不足以构成表格",
            checks=checks,
        )
    reason_parts.append(f"列数={max_cols} ✓")

    # ---- ③ 数据行 ≥ 3 ----
    data_rows = len(data) - 1 if len(data) > 1 else len(data)  # 排除首行表头
    checks["data_rows"] = data_rows
    if data_rows < CLASSIFIER_CONFIG["min_data_rows"]:
        return ClassifyResult(
            page=page_num, is_real_table=False, confidence=0.9,
            reason=f"数据行不足（{data_rows} < 3），仅 {len(data)} 行",
            checks=checks,
        )
    reason_parts.append(f"数据行={data_rows} ✓")

    # ---- ② 目录页排除 ----
    checks["is_toc"] = False

    # 检查 2a: 目录关键词
    if _contains_toc_keyword(data):
        checks["is_toc"] = True
        return ClassifyResult(
            page=page_num, is_real_table=False, confidence=0.9,
            reason="检测到目录页关键词，疑似目录页",
            checks=checks,
        )

    # 检查 2b: 高重复率列
    for c in range(max_cols):
        col_vals = []
        for r in data:
            if c < len(r):
                col_vals.append(str(r[c]).strip())
        dup_ratio = _column_duplicate_ratio(col_vals)
        if dup_ratio >= CLASSIFIER_CONFIG["toc_duplicate_ratio"] and len(col_vals) >= 3:
            checks["is_toc"] = True
            checks["toc_col"] = c
            checks["toc_dup_ratio"] = round(dup_ratio, 2)
            return ClassifyResult(
                page=page_num, is_real_table=False, confidence=0.85,
                reason=f"第{c+1}列重复率{dup_ratio:.1%}（≥{CLASSIFIER_CONFIG['toc_duplicate_ratio']:.0%}），疑似目录页或索引页",
                checks=checks,
            )

    # 检查 2c: 如果所有行首列值各不相同且看起来像章节标题（非数值），也怀疑是目录
    first_col_titles = []
    for r in data:
        if len(r) > 0 and str(r[0]).strip():
            first_col_titles.append(str(r[0]).strip())
    if len(first_col_titles) == len(data) and all(
        not _is_numeric_value(v) for v in first_col_titles
    ):
        # 所有第一列值都是文本且各不相同 → 可能目录
        pass  # 不强判为 false，因为很多表格的第一列也是文本

    # ---- ① 至少一列纯数值 ----
    has_numeric_col = False
    numeric_cols = []
    for c in range(max_cols):
        col_vals = []
        for r in data:
            if c < len(r):
                col_vals.append(str(r[c]).strip() if r[c] is not None else "")
        ratio = _column_numeric_ratio(col_vals)
        if ratio >= CLASSIFIER_CONFIG["numeric_column_ratio"]:
            has_numeric_col = True
            numeric_cols.append({"col": c, "ratio": round(ratio, 2)})

    checks["has_numeric_col"] = has_numeric_col
    checks["numeric_cols"] = numeric_cols

    if not has_numeric_col:
        # 宽松模式：如果任何一列数值占比 ≥ 30%，且数据行 ≥ 5，也放行
        for c in range(max_cols):
            col_vals = []
            for r in data:
                if c < len(r):
                    col_vals.append(str(r[c]).strip() if r[c] is not None else "")
            ratio = _column_numeric_ratio(col_vals)
            if ratio >= 0.30 and data_rows >= 5:
                has_numeric_col = True
                numeric_cols.append({"col": c, "ratio": round(ratio, 2), "relaxed": True})
                break

    if not has_numeric_col:
        return ClassifyResult(
            page=page_num, is_real_table=False, confidence=0.75,
            reason="所有列的数值占比均不满足阈值，可能是文本列表或非数据表格",
            checks=checks,
        )
    reason_parts.append(f"数值列={[nc['col']+1 for nc in numeric_cols]} ✓")

    # ---- 全部通过 → 真表格 ----
    return ClassifyResult(
        page=page_num, is_real_table=True, confidence=0.9,
        reason="; ".join(reason_parts),
        checks=checks,
    )


def classify_all(all_tables: List[dict]) -> List[ClassifyResult]:
    """对所有 pdf2docx 表格页进行批量分类

    Args:
        all_tables: processed_results['tables'] 列表

    Returns:
        [ClassifyResult, ...]
    """
    results = []
    for t in all_tables:
        page = t.get("page", 0)
        # 只处理 parse_status == 'success' 的页
        if t.get("parse_status") != "success":
            results.append(ClassifyResult(
                page=page, is_real_table=False, confidence=1.0,
                reason=f"parse_status={t.get('parse_status', 'unknown')}",
                checks={"parse_status": t.get("parse_status")},
            ))
            continue

        data = t.get("data", [])
        if not data:
            results.append(ClassifyResult(
                page=page, is_real_table=False, confidence=0.95,
                reason="data 为空",
                checks={},
            ))
            continue

        result = classify_page(data, page)
        results.append(result)

    return results
