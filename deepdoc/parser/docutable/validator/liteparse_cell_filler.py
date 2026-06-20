# -*- coding: utf-8 -*-
"""
liteparse 单元格填充器 — 用 liteparse 精确文本填充 pdf2docx 表格骨架

架构位置：
  Layer 3（代码层）：在 LLM 边界检测完成、pdf2docx 碎片合并之后，
  用 liteparse 的精确文本覆盖 pdf2docx 每个单元格的内容。

核心思路：
  - pdf2docx 提供表格骨架（行列网格、列数）
  - liteparse 提供单元格内容（精确文本，不会因 PDF→DOCX 转换出错）
  - 行匹配沿用 cell_differ 的标签文本匹配逻辑

与 cell_differ 的关系：
  - cell_differ: 报告差异（pdf2docx vs liteparse 不一致的单元格）
  - cell_filler: 用 liteparse 文本覆盖 pdf2docx 单元格（主动修复）

设计原则：
  - 零 API 成本，纯代码逻辑
  - 优先匹配行标签，再按 X 坐标分配列数据
  - 处理幽灵行（phantom）、缺失行（missing）、多余行（extra）
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Tuple

# 复用 cell_differ 的核心工具函数
from .cell_differ import (
    _cluster_items_by_y,
    _find_row_label,
    _normalize_for_search,
    infer_column_schema,
    _score_table_row,
    _merge_split_decimals,
)


# ============================================================
# 工具函数
# ============================================================

def _build_liteparse_items(text_items: List[dict]) -> List[dict]:
    """从 liteparse text_items 字典列表构建标准化 items，并合并被拆开的小数。"""
    items = []
    for ti in text_items:
        if isinstance(ti, dict):
            t = ti.get("text", "").strip()
            y0 = ti.get("y0", 0)
            y1 = ti.get("y1", 0)
            if t and y1 > y0:
                items.append({
                    "text": t,
                    "x0": ti.get("x0", 0),
                    "x1": ti.get("x1", 0),
                    "y0": y0,
                    "y1": y1,
                    "y_mid": (y0 + y1) / 2,
                })
    # 合并被 PDF 引擎拆开的小数后缀（如 "239" + ".85" → "239.85"）
    return _merge_split_decimals(items)


def _get_lp_item_y_mid(item: dict) -> float:
    """获取 liteparse item 的 Y 中点，兼容有/无 y_mid 字段。"""
    if "y_mid" in item:
        return item["y_mid"]
    return (item.get("y0", 0) + item.get("y1", 0)) / 2


def _match_pdf_row_to_lp_row(
    pdf_row: List[str],
    lp_rows: List[dict],
    used_indices: set,
) -> Optional[int]:
    """在 liteparse 行中查找与 pdf2docx 行标签匹配的行。

    匹配策略（改进版）：
    1. 标签长度 >= 4 → 子串匹配（降低误匹配率）
    2. 标签长度 < 4  → 仅精确文本匹配（短标签如"2024"太容易误匹配）
    3. 归一化子串匹配
    4. 归一化等值

    Returns:
        匹配到的 lp_rows 索引，或 None
    """
    label = _find_row_label(pdf_row)
    if not label:
        return None

    label_norm = _normalize_for_search(label)
    label_clean = label.strip()
    short_label = len(label_clean) < 4  # 短标签仅精确匹配

    # 第一遍：子串匹配（长标签）/ 精确文本匹配（短标签）
    for idx, row in enumerate(lp_rows):
        if idx in used_indices:
            continue
        for text in row["texts"]:
            if short_label:
                # 短标签仅精确文本匹配
                if label_clean == text:
                    return idx
            else:
                # 长标签才做子串匹配
                if label_clean in text:
                    return idx

    # 第二遍：归一化子串匹配（仅长标签）
    if not short_label:
        for idx, row in enumerate(lp_rows):
            if idx in used_indices:
                continue
            for norm_text in row["norm_texts"]:
                if label_norm and (label_norm in norm_text or norm_text in label_norm):
                    return idx

    # 第三遍：归一化等值（不限标签长度）
    for idx, row in enumerate(lp_rows):
        if idx in used_indices:
            continue
        if label_norm in row["norm_texts"]:
            return idx

    return None


def _is_numeric_text(text: str) -> bool:
    """判断文本是否为数值（含千分位、百分号、负号、括号负号）。"""
    import re
    t = text.strip().replace(",", "").replace("%", "")
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    return bool(re.match(r'^-?\d+\.?\d*$', t))


def _has_numeric_feature(texts: List[str]) -> bool:
    """判断一组文本是否主要是数据行特征（含数值）。"""
    if not texts:
        return False
    numeric_count = sum(1 for t in texts if _is_numeric_text(t))
    return numeric_count >= max(1, len(texts) * 0.3)


# ============================================================
# Y 范围估算：根据表格内容与 liteparse 的重叠度估算表格 Y 区域
# ============================================================

def _estimate_table_y_range(
    table_data: List[List[str]],
    lp_items: List[dict],
    max_sample_rows: int = 20,
) -> Tuple[Optional[float], Optional[float]]:
    """用表格前几行的内容匹配 liteparse items，估算表格的 Y 范围。

    思路：
    - 取表格前 max_sample_rows 行中所有非空单元格文本
    - 在 liteparse items 中查找匹配项
    - 取匹配项 Y 坐标的 min/max，加上 margin

    Returns:
        (y_min, y_max) 或 (None, None) 表示无法估算
    """
    if not lp_items or not table_data:
        return None, None

    # 收集表格中的标签文本（前几行的非空单元格内容）
    table_texts = set()
    for row in table_data[:max_sample_rows]:
        for cell in row:
            t = str(cell).strip()
            if t and len(t) >= 2:
                table_texts.add(t)

    if not table_texts:
        return None, None

    # 收集匹配到的 LP items 的 Y 坐标
    matched_ys = []
    for item in lp_items:
        item_text = item.get("text", "").strip()
        if not item_text:
            continue
        for tt in table_texts:
            # 双向子串匹配（table text in item text OR item text in table text）
            if tt in item_text or item_text in tt:
                matched_ys.append(_get_lp_item_y_mid(item))
                break

    if len(matched_ys) < 3:
        return None, None

    matched_ys.sort()
    # 滤除离群值（去掉首尾 10%）
    trim_start = max(0, int(len(matched_ys) * 0.1))
    trim_end = max(0, int(len(matched_ys) * 0.1))
    if len(matched_ys) - trim_start - trim_end < 2:
        return None, None
    core_ys = matched_ys[trim_start:len(matched_ys) - trim_end]

    y_min = core_ys[0]
    y_max = core_ys[-1]
    margin = max(60, (y_max - y_min) * 0.2)  # 至少 60pt 容差
    return max(0, y_min - margin), y_max + margin


# ============================================================
# 主函数：填充表格
# ============================================================

def _build_lp_x_to_pdf_col_map(
    lp_rows: List[dict],
    table_data: List[List[str]],
    resolved: Dict[int, int],
    out_map: Dict[float, int],
):
    """从最佳匹配行建立 LP 项 X 中点 → PDF 列号的映射。

    选中 resolved 中 LP 项最多的那对 (pdf_r, lp_r)，用内容匹配建立
    LP item 的 X 中点坐标到 PDF 列号的映射。后续稀疏行可据此将
    LP 项放置到正确的列。

    Args:
        lp_rows: liteparse 行列表
        table_data: pdf2docx 表格
        resolved: pdf_idx → lp_idx
        out_map: 输出字典，key=round(x_mid,1), value=pdf_col_idx
    """
    out_map.clear()

    # 找 item 最多的匹配行对
    best_lp_r = -1
    best_pdf_r = -1
    best_count = 0
    for pdf_r, lp_r in resolved.items():
        n = len(lp_rows[lp_r].get("items", []))
        if n > best_count:
            best_count = n
            best_lp_r = lp_r
            best_pdf_r = pdf_r

    if best_lp_r < 0 or best_pdf_r < 0:
        return

    lp_items = lp_rows[best_lp_r].get("items", [])
    pdf_row = table_data[best_pdf_r]

    used_pdf_cols: set = set()

    # 第一遍：内容匹配建立映射
    for lp_item in lp_items:
        lp_text = lp_item.get("text", "").strip()
        lp_x_mid = (lp_item.get("x0", 0) + lp_item.get("x1", 0)) / 2
        lp_x_rounded = round(lp_x_mid, 1)

        for c in range(len(pdf_row)):
            if c in used_pdf_cols:
                continue
            pdf_val = str(pdf_row[c]).strip()
            if not pdf_val:
                continue
            # 精确或子串匹配
            if pdf_val == lp_text or lp_text in pdf_val or pdf_val in lp_text:
                out_map[lp_x_rounded] = c
                used_pdf_cols.add(c)
                break

    # 第二遍：未匹配的参考项按 X 顺序分配到剩余列
    unmatched_ref = [
        it for it in lp_items
        if round((it.get("x0", 0) + it.get("x1", 0)) / 2, 1) not in out_map
    ]
    available_cols = [c for c in range(len(pdf_row)) if c not in used_pdf_cols]
    available_cols.sort()

    for i, lp_item in enumerate(unmatched_ref):
        if i >= len(available_cols):
            break
        lp_x_mid = (lp_item.get("x0", 0) + lp_item.get("x1", 0)) / 2
        out_map[round(lp_x_mid, 1)] = available_cols[i]


def _find_nearest_pdf_col(
    lp_x_mid: float,
    x_to_col: Dict[float, int],
    max_distance: float = 100.0,
) -> Optional[int]:
    """根据 LP 项的 X 中点，在参考映射中找最近的 PDF 列号。

    Args:
        lp_x_mid: LP 项的 X 中点
        x_to_col: round(x_mid, 1) → pdf_col_idx 映射
        max_distance: 最大允许距离 (pt)，超出返回 None

    Returns:
        PDF 列号，或 None
    """
    if not x_to_col:
        return None

    best_dist = float("inf")
    best_col = None

    for ref_x, col_idx in x_to_col.items():
        dist = abs(lp_x_mid - ref_x)
        if dist < best_dist:
            best_dist = dist
            best_col = col_idx

    if best_dist <= max_distance and best_col is not None:
        return best_col
    return None


def fill_table_with_liteparse(
    table_data: List[List[str]],
    liteparse_text_items: List[dict],
    table_page: int = 0,
) -> Tuple[List[List[str]], dict]:
    """用 liteparse 精确文本填充 pdf2docx 表格骨架。

    对每行 pdf2docx 数据：
    1. 在 liteparse 中匹配对应行（按行标签文本匹配）
    2. 匹配成功 → 用 liteparse 文本按 X 顺序替换该行
    3. 匹配失败 → 保留 pdf2docx 原值（可能是真实多余行）
    4. liteparse 有但 pdf2docx 没有的行 → 补充到结果末尾

    Args:
        table_data: pdf2docx 提取的 2D 表格
        liteparse_text_items: [{text, x0, y0, x1, y1}, ...]
        table_page: 表格所在页码（日志用）

    Returns:
        (filled_data, stats)
        - filled_data: 填充后的 2D 表格
        - stats: {"rows_replaced": N, "cells_changed": N, "rows_added": N,
                   "phantom_removed": N, "total_rows_before": N, "total_rows_after": N}
    """
    if not liteparse_text_items:
        return copy.deepcopy(table_data), {
            "rows_replaced": 0, "cells_changed": 0, "rows_added": 0,
            "phantom_removed": 0,
            "total_rows_before": len(table_data),
            "total_rows_after": len(table_data),
        }

    # ---- 1. 构建 liteparse 数据 ----
    items = _build_liteparse_items(liteparse_text_items)
    if not items:
        return copy.deepcopy(table_data), {
            "rows_replaced": 0, "cells_changed": 0, "rows_added": 0,
            "phantom_removed": 0,
            "total_rows_before": len(table_data),
            "total_rows_after": len(table_data),
        }

    # 按 Y 聚类为行
    lp_rows = _cluster_items_by_y(items)
    if not lp_rows:
        return copy.deepcopy(table_data), {
            "rows_replaced": 0, "cells_changed": 0, "rows_added": 0,
            "phantom_removed": 0,
            "total_rows_before": len(table_data),
            "total_rows_after": len(table_data),
        }

    pdf_cols = max((len(r) for r in table_data), default=0)
    if pdf_cols == 0:
        return copy.deepcopy(table_data), {
            "rows_replaced": 0, "cells_changed": 0, "rows_added": 0,
            "phantom_removed": 0,
            "total_rows_before": len(table_data),
            "total_rows_after": len(table_data),
        }

    # ---- 2. 行匹配 ----
    # pdf_row_index → lp_row_index (最佳匹配)
    row_matches: Dict[int, int] = {}
    # lp_row_index → [pdf_row_indices] (同一 LP 行被多个 PDF 行匹配)
    lp_to_pdf: Dict[int, List[int]] = {}

    for r, row in enumerate(table_data):
        if not row:
            continue
        label = _find_row_label(row)
        if not label:
            continue

        # 查找所有可能的 LP 匹配（改进版：短标签不分子串）
        label_norm = _normalize_for_search(label)
        label_clean = label.strip()
        short_label = len(label_clean) < 4

        candidates = []
        for lp_idx, lp_row in enumerate(lp_rows):
            for text in lp_row["texts"]:
                if short_label:
                    # 短标签仅精确文本匹配，避免"2024"误匹配所有含"2024年"的行
                    if label_clean == text:
                        candidates.append(lp_idx)
                        break
                else:
                    if label_clean in text:
                        candidates.append(lp_idx)
                        break
            if lp_idx in candidates:
                continue
            if not short_label:
                for norm_text in lp_row["norm_texts"]:
                    if label_norm and (label_norm in norm_text or norm_text in label_norm):
                        candidates.append(lp_idx)
                        break

        if candidates:
            for lp_idx in candidates:
                lp_to_pdf.setdefault(lp_idx, []).append(r)

    # ---- 3. 解析幽灵行 ----
    # 同一 LP 行被多个 PDF 行匹配 → 选择"最佳"行（非空值最多的），其余为幽灵行
    resolved: Dict[int, int] = {}  # pdf_idx → lp_idx
    phantom_rows: set = set()
    used_lp: set = set()

    for lp_idx, pdf_rows in lp_to_pdf.items():
        if len(pdf_rows) == 1:
            resolved[pdf_rows[0]] = lp_idx
            used_lp.add(lp_idx)
            continue

        # 多匹配 → 子集/重叠分析
        row_sets = {}
        for pdf_r in pdf_rows:
            vals = set()
            for c in range(1, len(table_data[pdf_r])):  # skip label col
                v = str(table_data[pdf_r][c]).strip()
                if v:
                    vals.add(v)
            row_sets[pdf_r] = vals

        # 找 primary（最多非空值的行）
        primary = max(pdf_rows, key=lambda r: len(row_sets.get(r, set())))

        for pdf_r in pdf_rows:
            if pdf_r == primary:
                continue
            set_r = row_sets.get(pdf_r, set())
            set_p = row_sets.get(primary, set())
            # 策略1：严格子集
            if set_r and set_r.issubset(set_p) and (set_p - set_r):
                phantom_rows.add(pdf_r)
            elif set_p and set_p.issubset(set_r) and (set_r - set_p):
                phantom_rows.add(primary)
                primary = pdf_r
            elif set_r and set_p:
                # 策略2：高重叠度检测（处理缺列导致的列错位）
                intersection = set_r & set_p
                union = set_r | set_p
                if union and len(intersection) / len(union) >= 0.6:
                    if len(set_r) <= len(set_p):
                        phantom_rows.add(pdf_r)
                    else:
                        phantom_rows.add(primary)
                        primary = pdf_r

        if primary not in phantom_rows:
            resolved[primary] = lp_idx
            used_lp.add(lp_idx)

    # ---- 4. 构建 LP 列 → PDF 列的 X 坐标映射 ----
    # 从匹配行中找一条 item 最多的 LP 行作为参考，建立 X 中点 → PDF 列号的映射
    lp_x_to_pdf_col: Dict[float, int] = {}
    _build_lp_x_to_pdf_col_map(lp_rows, table_data, resolved, lp_x_to_pdf_col)

    # ---- 5. 构建新表格数据 ----
    new_data = []
    rows_replaced = 0
    cells_changed = 0

    for r, row in enumerate(table_data):
        if r in phantom_rows:
            # 幽灵行：跳过（不加入结果）
            continue

        if r in resolved:
            lp_idx = resolved[r]
            lp_row = lp_rows[lp_idx]

            lp_texts = lp_row["texts"]  # 已按 X 排序
            lp_items = lp_row.get("items", [])

            new_row = list(row)  # 从 pdf2docx 列结构开始
            matched_lp_idx = set()

            # 第一轮：精确匹配（LP 文本与 PDF 单元格完全相同）
            for c in range(len(row)):
                pdf_val = str(row[c]).strip()
                if not pdf_val:
                    continue
                for i, lp_text in enumerate(lp_texts):
                    if i in matched_lp_idx:
                        continue
                    if lp_text == pdf_val:
                        matched_lp_idx.add(i)
                        break

            # 第二轮：子串匹配（LP 文本包含 PDF 文本，如千分位差异）
            for c in range(len(row)):
                pdf_val = str(row[c]).strip()
                if not pdf_val:
                    continue
                for i, lp_text in enumerate(lp_texts):
                    if i in matched_lp_idx:
                        continue
                    if pdf_val in lp_text:
                        if lp_text != pdf_val:
                            new_row[c] = lp_text
                            cells_changed += 1
                        matched_lp_idx.add(i)
                        break

            # 第三轮：归一化数值匹配（处理千分位、百分号、括号等差异）
            for c in range(len(row)):
                pdf_val = str(row[c]).strip()
                if not pdf_val:
                    continue
                pdf_norm = _normalize_for_search(pdf_val)
                if not pdf_norm:
                    continue
                for i, lp_text in enumerate(lp_texts):
                    if i in matched_lp_idx:
                        continue
                    lp_norm = _normalize_for_search(lp_text)
                    if pdf_norm == lp_norm:
                        if lp_text != pdf_val:
                            new_row[c] = lp_text
                            cells_changed += 1
                        matched_lp_idx.add(i)
                        break

            # 第四轮：X 坐标驱动列映射 — 剩余 LP 项按 X 位置填入对应列
            unmatched = [
                (i, lp_texts[i]) for i in range(len(lp_texts))
                if i not in matched_lp_idx
            ]
            if unmatched and lp_x_to_pdf_col:
                for lp_i, lp_text in unmatched:
                    # 获取该 LP item 的 X 中点
                    lp_x_mid = None
                    if lp_i < len(lp_items):
                        lp_x_mid = (lp_items[lp_i].get("x0", 0) + lp_items[lp_i].get("x1", 0)) / 2

                    if lp_x_mid is None:
                        continue

                    # 在参考映射中找最近的 X → PDF 列号
                    target_col = _find_nearest_pdf_col(lp_x_mid, lp_x_to_pdf_col)

                    if target_col is not None and target_col < len(new_row):
                        if not str(new_row[target_col]).strip():
                            # 目标列为空 → 填入
                            new_row[target_col] = lp_text
                            cells_changed += 1
                        elif _normalize_for_search(str(new_row[target_col]).strip()) == _normalize_for_search(lp_text):
                            # 归一化后相同 → 替换（如千分位差异）
                            if lp_text != str(new_row[target_col]).strip():
                                new_row[target_col] = lp_text
                                cells_changed += 1
                        # 否则目标列已有不同值 → 不覆盖
            elif unmatched:
                # 兜底：无 X 映射时，左侧标签保留，数值按类型填入
                num_items = [(i, t) for i, t in unmatched if _is_numeric_text(t)]
                txt_items = [(i, t) for i, t in unmatched if not _is_numeric_text(t)]
                for c in range(len(row)):
                    if str(new_row[c]).strip():
                        continue
                    if num_items:
                        item = num_items.pop(0)
                    elif txt_items:
                        item = txt_items.pop(0)
                    else:
                        break
                    new_row[c] = item[1]
                    cells_changed += 1

            rows_replaced += 1
            new_data.append(new_row)
        else:
            # 未匹配到 LP 行 → 保留原值
            new_data.append(list(row))

    # ---- 5. 补充 liteparse 中有但 pdf2docx 没有的行 ----
    rows_added = 0
    # 推断列签名（用于行评分）
    col_schema = infer_column_schema(lp_rows)

    for lp_idx, lp_row in enumerate(lp_rows):
        if lp_idx in used_lp:
            continue

        texts = lp_row["texts"]

        # 跳过单文本短行（页面标题等）
        if len(texts) <= 1 and all(len(t) < 30 for t in texts):
            continue

        # 长句说明文字（>80字符、无数值特征）→ 跳过
        total_len = sum(len(t) for t in texts)
        if total_len > 80 and not _has_numeric_feature(texts):
            continue

        # 用列签名分值制判定是否为数据行
        # 得分 ≥ 2 → 是数据行（支持稀疏行如"净利差  |  34,180,088"）
        score = _score_table_row(texts, col_schema)
        if score < 2:
            continue

        new_row = list(texts)
        while len(new_row) < pdf_cols:
            new_row.append("")
        if len(new_row) > pdf_cols:
            new_row = new_row[:pdf_cols]
        new_data.append(new_row)
        rows_added += 1

    stats = {
        "rows_replaced": rows_replaced,
        "cells_changed": cells_changed,
        "rows_added": rows_added,
        "phantom_removed": len(phantom_rows),
        "total_rows_before": len(table_data),
        "total_rows_after": len(new_data),
    }

    if any(v > 0 for v in stats.values() if v != stats.get("total_rows_before", 0)):
        print(f"  [填充] P{table_page}: {rows_replaced}行替换, {cells_changed}格变化, "
              f"{len(phantom_rows)}幽灵行移除, {rows_added}行补充 "
              f"({stats['total_rows_before']}→{stats['total_rows_after']}行)")

    return new_data, stats


# ============================================================
# 批量填充（对外接口）
# ============================================================

def fill_all_tables_with_liteparse(
    tables: List[dict],
    liteparse_data: dict,
) -> Tuple[List[dict], dict]:
    """对全部表格执行 liteparse 文本填充。

    Args:
        tables: pdf2docx 提取的表格列表（已合并碎片后）
        liteparse_data: liteparse ParseResult.to_dict()

    Returns:
        (filled_tables, aggregate_stats)
    """
    if not liteparse_data:
        return tables, {}

    from codes.table_validator.table_boundary import _get_liteparse_page

    total_stats = {
        "tables_filled": 0,
        "total_rows_replaced": 0,
        "total_cells_changed": 0,
        "total_rows_added": 0,
        "total_phantom_removed": 0,
    }

    for table in tables:
        if not table.get("data"):
            continue

        page_num = table.get("page", 0)
        merged_pages = table.get("_merged_from_pages", [page_num])

        # 跨页合并表：收集所有源页的 text_items
        all_text_items = []
        has_scoped = False  # 是否有精确区域限定
        for pn in sorted(merged_pages):
            lp_page = _get_liteparse_page(liteparse_data, pn)
            if not lp_page:
                continue
            # 优先使用 scoped items（同页多表区域限定）
            scoped = table.get("_liteparse_items_per_page", {}).get(str(pn))
            if scoped:
                all_text_items.extend(scoped)
                has_scoped = True
            else:
                all_text_items.extend(lp_page.get("text_items", []))

        if not all_text_items:
            # 兜底：单页
            lp_page = _get_liteparse_page(liteparse_data, page_num)
            if not lp_page:
                continue
            text_items = table.get("_liteparse_items")
            if not text_items:
                text_items = lp_page.get("text_items", [])
            if not text_items:
                continue
            has_scoped = bool(table.get("_liteparse_items"))
        else:
            text_items = all_text_items

        # 🔧 无精确区域限定时 → 用表格内容估算 Y 范围过滤 LP items
        # 防止整页无关文本（图例、说明、注释等）被当作表格数据候选人
        if not has_scoped and text_items:
            y_min, y_max = _estimate_table_y_range(table["data"], text_items)
            if y_min is not None and y_max is not None:
                filtered = [
                    item for item in text_items
                    if y_min <= _get_lp_item_y_mid(item) <= y_max
                ]
                if filtered:
                    text_items = filtered

        new_data, stats = fill_table_with_liteparse(
            table["data"], text_items, page_num
        )

        if stats["cells_changed"] > 0 or stats["rows_added"] > 0 or stats["phantom_removed"] > 0:
            # 保存原始数据用于回溯
            if "original_data" not in table:
                table["original_data"] = copy.deepcopy(table["data"])
            table["_pre_fill_data"] = copy.deepcopy(table["data"])
            table["data"] = new_data
            table["rows"] = len(new_data)

            total_stats["tables_filled"] += 1
            total_stats["total_rows_replaced"] += stats["rows_replaced"]
            total_stats["total_cells_changed"] += stats["cells_changed"]
            total_stats["total_rows_added"] += stats["rows_added"]
            total_stats["total_phantom_removed"] += stats["phantom_removed"]

    if total_stats["tables_filled"] > 0:
        print(f"  [填充] 汇总: {total_stats['tables_filled']}张表, "
              f"{total_stats['total_cells_changed']}个单元格已用 liteparse 文本覆盖, "
              f"{total_stats['total_phantom_removed']}幽灵行移除, "
              f"{total_stats['total_rows_added']}行补充")

    return tables, total_stats
