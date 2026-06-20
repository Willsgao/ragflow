# -*- coding: utf-8 -*-
"""
行/列层级差异检测器

对比 pdf2docx 表格与 liteparse 同页文本的差异。

核心设计：
- 内部聚类：liteparse text_items 按 Y 坐标聚类成行（同一数据源内，不跨源比较坐标）
- 跨源对齐：行对齐靠首列标签文本匹配，列对齐靠同行内相对位置（索引），不依赖绝对坐标
- 差异类型：可疑值（同行同列值不一致）、多余行、缺失行、多余列、缺失列
- 列类型签名：利用同列数据类型一致性约束 Y 聚类和行数据判定
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Dict, List, Tuple, Optional, Set


# ============================================================
# 工具函数
# ============================================================

def _normalize_for_search(val: str) -> str:
    """归一化数值，用于在 liteparse 文本中查找匹配。

    去掉千分位逗号、中文空格、英文空格、括号转负号。
    """
    s = val.strip().replace(",", "").replace(" ", "").replace("\u3000", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return s


def _merge_split_decimals(items: List[dict]) -> List[dict]:
    """合并被 PDF 引擎拆开的小数点后缀。

    PDF 引擎（liteparse）有时会将 "239.85"、"1.44%" 等数值拆成整数和
    小数后缀两个独立的 text_item，原因是整数和小数部分使用了不同的字体/字间距。

    本函数在 Y 聚类之前修复此问题：
    - 找到以 "." 开头后跟数字的 item（如 ".85"、".44%"）
    - 在同行同高度内找左侧最近的纯整数 item（如 "239"、"1"）
    - 如果 X 间距在合理范围内（≤ 该 item 宽度的 1.5 倍），合并

    Args:
        items: 标准化后的 text_items 列表（已有 x0, x1, y_mid 等字段）

    Returns:
        合并后的 items 列表（被合并的小数后缀 item 已移除）
    """
    if len(items) < 2:
        return items

    decimal_suffix_pattern = re.compile(r'^\.\d+\D?$')

    # 识别小数后缀和候选左侧整数
    decimal_suffixes = []  # [(idx, item), ...]
    integer_candidates = []  # [(idx, item), ...]

    for i, it in enumerate(items):
        text = it.get("text", "").strip()
        if not text:
            continue
        if decimal_suffix_pattern.match(text):
            decimal_suffixes.append((i, it))
        elif text and text[-1].isdigit():
            # 结尾是数字的可能是整数部分（纯数字或含千分位逗号）
            clean = text.replace(",", "")
            if re.match(r'^-?\d+$', clean):
                integer_candidates.append((i, it))

    if not decimal_suffixes:
        return items

    # 对每个小数后缀，找最近左侧整数
    merged_indices = set()  # 要移除的小数后缀索引
    merged_items = []       # 新增的合并后 item

    for ds_idx, ds_item in decimal_suffixes:
        ds_x0 = ds_item.get("x0", 0)
        ds_x1 = ds_item.get("x1", 0)
        ds_y_mid = ds_item.get("y_mid", 0)
        ds_y0 = ds_item.get("y0", 0)
        ds_y1 = ds_item.get("y1", 0)

        best_int_idx = None
        best_int_item = None
        best_x_gap = float("inf")

        for int_idx, int_item in integer_candidates:
            if int_idx == ds_idx or int_idx in merged_indices:
                continue
            int_x1 = int_item.get("x1", 0)
            int_y_mid = int_item.get("y_mid", 0)

            # Y 检查：必须在同一行（Y 中点差 ≤ 3pt）
            if abs(ds_y_mid - int_y_mid) > 3.0:
                continue

            # X 检查：必须在小数点左侧（int_x1 ≤ ds_x0）
            if int_x1 > ds_x0 + 2.0:
                continue

            gap = ds_x0 - int_x1
            if gap < 0:
                gap = 0  # 允许极小重叠

            # 间距必须合理：不超过该整数 item 自身宽度的 1.5 倍
            int_width = int_item.get("x1", 0) - int_item.get("x0", 0)
            max_gap = max(int_width * 1.5, 15.0)
            if gap > max_gap:
                continue

            if gap < best_x_gap:
                best_x_gap = gap
                best_int_idx = int_idx
                best_int_item = int_item

        if best_int_item is not None:
            # 合并：生成新 item
            merged_text = best_int_item["text"] + ds_item["text"]
            merged_items.append({
                "text": merged_text,
                "x0": best_int_item["x0"],
                "x1": ds_x1,  # 延伸到小数后缀的右边界
                "y0": min(best_int_item.get("y0", ds_y0), ds_y0),
                "y1": max(best_int_item.get("y1", ds_y1), ds_y1),
                "y_mid": (best_int_item.get("y_mid", ds_y_mid) + ds_y_mid) / 2,
            })
            merged_indices.add(best_int_idx)
            merged_indices.add(ds_idx)

    if not merged_indices:
        return items

    # 构建结果：保留未被合并的 item + 合并后的新 item
    result = []
    for i, it in enumerate(items):
        if i in merged_indices:
            continue
        result.append(it)

    result.extend(merged_items)

    # 按 Y, X 重新排序
    result.sort(key=lambda it: (it["y_mid"], it["x0"]))

    return result


# ============================================================
# 列类型分类 & 列签名推断（新增）
# ============================================================

def classify_item_type(text: str) -> str:
    """将单个文本项分类为语义类型。

    分类体系：
    - "label"   : 纯中文/含中文的财务术语（首列行标签）
    - "amount"  : 大额数值（≥4位有效数字，含千分位逗号），如 25,228,241
    - "number"  : 普通数值（<4位有效数字），如 589,882
    - "percent" : 以 % 结尾
    - "ratio"   : 小数型比率（无%，纯小数如 1.51、3.43）
    - "empty"   : 空字符串/空白
    - "mixed"   : 中文+数字混合（表头行常见）
    """
    t = text.strip() if text else ""
    if not t:
        return "empty"

    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', t))
    has_digit = bool(re.search(r'\d', t))

    # 百分比（优先，避免被其他数值类型误判）
    if t.endswith('%'):
        return "percent"

    # 中文+数字混合（如"2024年1-6月"、"变化+/(-)"）
    if has_chinese and has_digit:
        return "mixed"

    # 纯中文 → 标签
    if has_chinese:
        return "label"

    # 数值判断
    if has_digit:
        # 去掉格式化符号，提取纯净数字
        clean = t.replace(',', '').replace(' ', '').replace('\u3000', '')
        # 处理括号负号
        if clean.startswith('(') and clean.endswith(')'):
            clean = '-' + clean[1:-1]
        # 提取数字部分
        num_match = re.search(r'-?\d+\.?\d*', clean)
        if num_match:
            num_str = num_match.group()
            try:
                val = float(num_str)
                digit_count = len(num_str.replace('-', '').replace('.', ''))
                if digit_count >= 6:  # ≥6位有效数字 → 大额数值
                    return "amount"
                elif abs(val) < 100 and '.' in num_str:  # 小数比率
                    return "ratio"
                else:
                    return "number"
            except ValueError:
                return "mixed"

    # 兜底
    return "mixed"


def _estimate_font_size(items: List[dict]) -> float:
    """从 items 的 Y 跨度估算字体大小（中位数）。"""
    heights = [it["y1"] - it["y0"] for it in items if it.get("y1", 0) > it.get("y0", 0)]
    if not heights:
        return 10.0  # 默认 10pt
    heights.sort()
    mid = len(heights) // 2
    return heights[mid] if len(heights) % 2 == 1 else (heights[mid - 1] + heights[mid]) / 2


def _compute_dynamic_y_threshold(items: List[dict], fallback: float = 5.0) -> float:
    """根据字体大小动态计算 Y 聚类阈值。

    原则：阈值 = max(font_size * 0.5, 4.0)
    9pt 字体 → 4.5pt，12pt 字体 → 6pt
    """
    font_size = _estimate_font_size(items)
    return max(font_size * 0.5, 4.0)


def _find_first_col_baseline(rows: List[dict]) -> Optional[float]:
    """检测首列行标签的 X 基线位置（众数）。

    大多数数据行的第一列标签在相同 X 位置对齐。
    返回该 X 位置，用于后续强制拆行。
    """
    first_x0s = []
    for row in rows:
        texts = row.get("texts", [])
        items = row.get("items", [])
        if not items:
            continue
        # 第一个 item 的 x0
        x0 = items[0].get("x0", 0)
        # 只统计像行标签的（首项含中文）
        if texts and re.search(r'[\u4e00-\u9fff]', texts[0]):
            first_x0s.append(round(x0, 1))

    if len(first_x0s) < 3:
        return None

    counter = Counter(first_x0s)
    baseline, count = counter.most_common(1)[0]
    if count >= max(3, len(first_x0s) * 0.4):
        return baseline
    return None


def _estimate_column_x_ranges_from_items(
    items: List[dict],
    gap_multiplier: float = 2.5,
) -> List[Tuple[float, float]]:
    """从一批 items 的 X 坐标估计列范围。

    对所有 item 的 x0 做自适应聚类，每个聚类 = 一个列。
    聚类后合并稀疏列（item 数过少的列），防止缩进标签等偏移产生幽灵列。
    """
    if not items:
        return []

    all_x_pairs = [(it.get("x0", 0), it.get("x1", 0)) for it in items
                   if it.get("x1", 0) > it.get("x0", 0)]
    if not all_x_pairs:
        return []

    all_x_pairs.sort(key=lambda p: p[0])
    x0s = [p[0] for p in all_x_pairs]
    gaps = [x0s[i + 1] - x0s[i] for i in range(len(x0s) - 1) if x0s[i + 1] > x0s[i]]
    if not gaps:
        return [(all_x_pairs[0][0], all_x_pairs[-1][1])]

    avg_gap = sum(gaps) / len(gaps)
    threshold = max(avg_gap * gap_multiplier, 8.0)

    clusters = []
    current = [all_x_pairs[0]]
    for i in range(1, len(all_x_pairs)):
        if all_x_pairs[i][0] - current[-1][0] > threshold:
            clusters.append(current)
            current = [all_x_pairs[i]]
        else:
            current.append(all_x_pairs[i])
    if current:
        clusters.append(current)

    col_ranges = [(min(p[0] for p in c), max(p[1] for p in c)) for c in clusters]

    LOG = logging.getLogger("table_reconstructor")
    LOG.debug(
        "  [cell_differ 列估计] x0聚类得出 %d 列: %s",
        len(col_ranges),
        ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
    )

    # 合并稀疏列：统计每列的 item 数，过少的列合并到最近非稀疏列
    if len(col_ranges) > 1:
        col_item_counts = [0] * len(col_ranges)
        for x0, _x1 in all_x_pairs:
            for ci, (cx0, cx1) in enumerate(col_ranges):
                if cx0 <= x0 <= cx1:
                    col_item_counts[ci] += 1
                    break

        merge_threshold = max(int(max(col_item_counts) * 0.15), 3)
        sparse_mask = [c < merge_threshold for c in col_item_counts]
        if any(sparse_mask):
            for ci in range(len(col_ranges)):
                if sparse_mask[ci]:
                    LOG.debug(
                        "  [cell_differ 稀疏列] 列#%d [%.0f,%.0f] items=%d/%d — 合并",
                        ci, col_ranges[ci][0], col_ranges[ci][1],
                        col_item_counts[ci], max(col_item_counts),
                    )
            centers = [(x0 + x1) / 2 for x0, x1 in col_ranges]
            expanded = list(col_ranges)
            sparse_set = {i for i, s in enumerate(sparse_mask) if s}
            for si in sorted(sparse_set, reverse=True):
                best_j = -1
                best_dist = float("inf")
                for j in range(len(expanded)):
                    if j in sparse_set:
                        continue
                    dist = abs(centers[si] - centers[j])
                    if dist < best_dist:
                        best_dist = dist
                        best_j = j
                if best_j >= 0:
                    x0_j, x1_j = expanded[best_j]
                    x0_i, x1_i = expanded[si]
                    expanded[best_j] = (min(x0_j, x0_i), max(x1_j, x1_i))
            col_ranges = [expanded[i] for i in range(len(expanded)) if i not in sparse_set]
            LOG.debug(
                "  [cell_differ 稀疏列] 合并完成 → %d 列: %s",
                len(col_ranges),
                ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
            )

    return col_ranges


def _x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """计算两个 X 区间的 IoU（交并比）。"""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    if union <= 0:
        return 0.0
    return inter / union


def _assign_item_to_col(
    x0: float, x1: float, col_ranges: List[Tuple[float, float]]
) -> Optional[int]:
    """将 item 的 X 区间分配到最匹配的列索引。

    优先 IoU 匹配，其次中心点归属。
    """
    best_idx = None
    best_iou = 0.0

    for idx, (cx0, cx1) in enumerate(col_ranges):
        iou = _x_overlap(x0, x1, cx0, cx1)
        if iou > best_iou:
            best_iou = iou
            best_idx = idx

    if best_iou >= 0.5:
        return best_idx

    # 回退：中心点归属
    cx = (x0 + x1) / 2
    for idx, (cx0, cx1) in enumerate(col_ranges):
        if cx0 <= cx <= cx1:
            return idx

    return None


def infer_column_schema(
    lp_rows: List[dict],
) -> List[dict]:
    """从 liteparse 行推断列的语义签名。

    流程：
    1. 用所有行的 item X 坐标估计列范围
    2. 将每个 item 分配到对应的列
    3. 统计每列的类型分布，投票出 dominant_type
    4. 记录每列在数据行（跳过前2行表头）中的类型分布

    Returns:
        [{"x0": float, "x1": float, "dominant_type": str,
          "type_counts": {type: count}, ...}, ...]
    """
    if not lp_rows:
        return []

    # 收集所有 items
    all_items = []
    for row in lp_rows:
        all_items.extend(row.get("items", []))

    # 估计列 X 范围
    col_ranges = _estimate_column_x_ranges_from_items(all_items)
    if not col_ranges:
        return []

    # 统计每列的类型（跳过前 2 行表头，用数据行统计更准确）
    col_types: Dict[int, List[str]] = {i: [] for i in range(len(col_ranges))}

    for row in lp_rows:
        for it in row.get("items", []):
            x0 = it.get("x0", 0)
            x1 = it.get("x1", 0)
            text = it.get("text", "")
            if not text.strip():
                continue
            col_idx = _assign_item_to_col(x0, x1, col_ranges)
            if col_idx is not None:
                item_type = classify_item_type(text)
                col_types[col_idx].append(item_type)

    # 投票出每列的 dominant type
    schema = []
    for idx in range(len(col_ranges)):
        types = col_types.get(idx, [])
        if types:
            counter = Counter(types)
            dominant, dom_count = counter.most_common(1)[0]
            # 只有出现次数 ≥ 3 或占比 ≥ 30% 才确立类型
            if dom_count < 3 and dom_count / len(types) < 0.3:
                dominant = "mixed"
        else:
            dominant = "empty"

        schema.append({
            "x0": col_ranges[idx][0],
            "x1": col_ranges[idx][1],
            "dominant_type": dominant,
            "type_counts": dict(Counter(types)) if types else {},
            "total_items": len(types),
        })

    return schema


def _split_rows_by_column_conflict(
    rows: List[dict],
    col_schema: Optional[List[dict]] = None,
) -> List[dict]:
    """检测并拆分因列类型冲突而错误合并的行。

    核心规则：同一列（同一 X 区间）内不能有 2 个 item 属于同一数据行。
    如果发现冲突 → 按 Y 在最紧密间隙处拆分为两行。

    Args:
        rows: Y 聚类后的行列表
        col_schema: 列签名（可选，不传则自动推断）

    Returns:
        拆分后的行列表
    """
    if not rows or len(rows) < 1:
        return rows

    # 如果没有列签名，从行中推断
    if col_schema is None:
        col_schema = infer_column_schema(rows)
    if not col_schema:
        return rows

    col_ranges = [(c["x0"], c["x1"]) for c in col_schema]
    if not col_ranges:
        return rows

    new_rows = []

    for row in rows:
        items = row.get("items", [])
        if len(items) <= 2:
            new_rows.append(row)
            continue

        # 将 items 分配到列
        col_to_items: Dict[int, List[dict]] = {}
        for it in items:
            x0 = it.get("x0", 0)
            x1 = it.get("x1", 0)
            col_idx = _assign_item_to_col(x0, x1, col_ranges)
            if col_idx is not None:
                col_to_items.setdefault(col_idx, []).append(it)

        # 检查是否有列冲突（同一列有多个 item）
        conflict_cols = {c: its for c, its in col_to_items.items() if len(its) > 1}
        if not conflict_cols:
            new_rows.append(row)
            continue

        # 取冲突最严重的列（item 数最多），按 Y 拆分
        worst_col = max(conflict_cols, key=lambda c: len(conflict_cols[c]))
        conflict_items = sorted(conflict_cols[worst_col], key=lambda it: it["y_mid"])

        # 找到冲突 items 之间的最大 Y 间隙，以此为拆分点
        split_y = None
        max_gap = 0.0
        for i in range(len(conflict_items) - 1):
            gap = conflict_items[i + 1]["y_mid"] - conflict_items[i]["y_mid"]
            if gap > max_gap:
                max_gap = gap
                # 拆分点在两个冲突 item 的中点
                split_y = (conflict_items[i]["y1"] + conflict_items[i + 1]["y0"]) / 2

        if split_y is None or max_gap < 2.0:
            # 间隙太小 → 保留原样（可能是真的同一行副列）
            new_rows.append(row)
            continue

        # 按 split_y 拆分为上下两行
        upper_items = [it for it in items if it["y_mid"] <= split_y]
        lower_items = [it for it in items if it["y_mid"] > split_y]

        if upper_items:
            new_rows.append(_build_row_dict(upper_items))
        if lower_items:
            new_rows.append(_build_row_dict(lower_items))

    return new_rows


def _score_table_row(
    texts: List[str],
    col_schema: Optional[List[dict]] = None,
) -> int:
    """用列签名分值制判定一行是否为表格数据行。

    替代原有的刚性 len(texts)>=3 判定。即使只有 2 个 item，
    只要匹配对应列的 dominant type，也能识别为有效数据行。

    分值规则：
    - 列数分：≥4列 +3, ≥3列 +2, ==2列 +1
    - 数值分：数值占比≥50% +3, ≥30% +2, >0 +1
    - 行标签分：首列是中文标签且含常见财务关键词 +2
    - 列签名匹配分：每匹配一个列类型 +1（最多+3）

    Returns:
        总分，≥2 判定为数据行
    """
    if not texts:
        return 0

    score = 0
    non_empty = [t for t in texts if t.strip()]

    # --- 列数分 ---
    n = len(non_empty)
    if n >= 4:
        score += 3
    elif n >= 3:
        score += 2
    elif n == 2:
        score += 1  # 不再直接跳过 2 列行

    # --- 数值分 ---
    numeric_count = sum(1 for t in non_empty if classify_item_type(t) in ("amount", "number", "percent", "ratio"))
    numeric_ratio = numeric_count / max(n, 1)
    if numeric_ratio >= 0.5:
        score += 3
    elif numeric_ratio >= 0.3:
        score += 2
    elif numeric_count > 0:
        score += 1

    # --- 行标签分：首列是中文财务术语 ---
    if non_empty:
        first_type = classify_item_type(non_empty[0])
        first_text = non_empty[0]
        if first_type == "label" or first_type == "mixed":
            # 检查是否像财务行标签
            financial_kw = [
                '资产', '负债', '收入', '支出', '费用', '利润', '成本', '收益',
                '贷款', '存款', '投资', '利息', '总额', '净额', '减值', '拨备',
                '资本', '权益', '现金', '同业', '金融', '债券', '借款', '回购',
                '发放', '存放', '吸收', '发行', '拆出', '拆入', '卖出', '买入',
                '生息', '计息', '非生息', '非计息',
            ]
            if any(kw in first_text for kw in financial_kw):
                score += 2

    # --- 列签名匹配分 ---
    if col_schema and len(col_schema) >= 2:
        match_count = 0
        for i, t in enumerate(non_empty):
            if t.strip() and i < len(col_schema):
                expected = col_schema[i].get("dominant_type", "mixed")
                actual = classify_item_type(t)
                if actual == expected:
                    match_count += 1
                elif actual == "empty":
                    pass  # 空值不扣分
        # 匹配分上限 3
        score += min(match_count, 3)

    return score


def _cluster_items_by_y(
    items: List[dict],
    threshold: float = 5.0,
    column_schema: Optional[List[dict]] = None,
    use_dynamic_threshold: bool = True,
    detect_first_col_baseline: bool = True,
    split_by_column_conflict: bool = True,
    merge_adjacent_rows: bool = True,
) -> List[dict]:
    """将 liteparse text_items 按 Y 坐标聚类为逻辑行。

    内部聚类，只使用 liteparse 自己的坐标系。

    增强策略（v2）：
    - 动态 Y 阈值：根据字体大小自适应
    - 首列基线检测：利用行标签 X 对齐特征辅助拆行
    - 列类型冲突检测：同一列不能有 2 个 item → 强制拆分

    Args:
        items: [{"text": ..., "y_mid": ..., "y0": ..., "y1": ..., "x0": ...}, ...]
        threshold: Y 坐标聚类阈值 (pt)，默认 5.0。当 use_dynamic_threshold=True
                   时，此值为 fallback
        column_schema: 预计算的列签名，用于列冲突检测
        use_dynamic_threshold: 是否启用动态阈值
        detect_first_col_baseline: 是否启用手首列基线检测
        split_by_column_conflict: 是否启用列类型冲突拆分

    Returns:
        按 Y 排序的行列表，每行为 {"items": [...], "y_min", "y_max",
        "texts_by_x": [...]} ，items 已按 X 排序
    """
    if not items:
        return []

    # --- 动态阈值 ---
    effective_threshold = threshold
    if use_dynamic_threshold:
        effective_threshold = _compute_dynamic_y_threshold(items, fallback=threshold)

    # --- 第一遍：标准 Y 聚类 ---
    sorted_items = sorted(items, key=lambda it: it["y_mid"])
    rows = []
    current_row = [sorted_items[0]]
    current_y = sorted_items[0]["y_mid"]

    for it in sorted_items[1:]:
        if abs(it["y_mid"] - current_y) <= effective_threshold:
            current_row.append(it)
        else:
            row_dict = _build_row_dict(current_row)
            rows.append(row_dict)
            current_row = [it]
            current_y = it["y_mid"]

    if current_row:
        rows.append(_build_row_dict(current_row))

    # --- 第二遍：首列 X 基线检测 ---
    if detect_first_col_baseline and len(rows) >= 3:
        baseline = _find_first_col_baseline(rows)
        if baseline is not None:
            refined = []
            for row in rows:
                items_in_row = row.get("items", [])
                if len(items_in_row) <= 1:
                    refined.append(row)
                    continue

                # 检测行内是否有多个 item 的 x0 接近基线
                near_baseline = [
                    it for it in items_in_row
                    if abs(it.get("x0", 0) - baseline) <= 10.0
                ]
                if len(near_baseline) <= 1:
                    refined.append(row)
                    continue

                # 多个 item 在基线位置 → 强制拆行
                near_baseline.sort(key=lambda it: it["y_mid"])
                split_items_map: Dict[int, List[dict]] = {}
                for it in items_in_row:
                    assigned = False
                    for bi, base_it in enumerate(near_baseline):
                        if abs(it["y_mid"] - base_it["y_mid"]) <= effective_threshold:
                            split_items_map.setdefault(bi, []).append(it)
                            assigned = True
                            break
                    if not assigned:
                        # 归入 Y 最近的基线组
                        best_bi = min(
                            range(len(near_baseline)),
                            key=lambda i: abs(it["y_mid"] - near_baseline[i]["y_mid"])
                        )
                        split_items_map.setdefault(best_bi, []).append(it)

                for bi in sorted(split_items_map.keys()):
                    refined.append(_build_row_dict(split_items_map[bi]))

            rows = refined

    # --- 第三遍：列类型冲突拆分 ---
    if split_by_column_conflict and len(rows) >= 2:
        if column_schema is None:
            column_schema = infer_column_schema(rows)
        rows = _split_rows_by_column_conflict(rows, column_schema)

    # --- 第四遍：相邻行合并（修复 Y 聚类阈值过紧导致的同行拆分） ---
    if merge_adjacent_rows and len(rows) >= 2:
        rows = _merge_adjacent_rows_with_same_label(rows)

    return rows


def _build_row_dict(row_items: List[dict]) -> dict:
    """构建单行的结构化字典。"""
    # 按 X 坐标排序（从左到右）
    sorted_items = sorted(row_items, key=lambda it: it.get("x0", 0))

    return {
        "items": sorted_items,
        "y_min": min(it["y0"] for it in sorted_items),
        "y_max": max(it["y1"] for it in sorted_items),
        "texts": [it["text"] for it in sorted_items],
        "norm_texts": [_normalize_for_search(it["text"]) for it in sorted_items],
        # 列的 X 范围，用于后续列对齐参考（不用于跨源比较）
        "col_x_ranges": [
            (it.get("x0", 0), it.get("x1", 0)) for it in sorted_items
        ],
    }


def _merge_adjacent_rows_with_same_label(rows: List[dict]) -> List[dict]:
    """合并相邻的、首列标签相同且列不冲突的行。

    场景：Y 聚类阈值过紧时，同一逻辑行被拆成 2 行（如合并单元格的标签
    居中偏移导致 Y 断开）。合并条件：
    1. 相邻两行的第一列标签相同（或其中一行为空）
    2. 两行在同一 X 位置没有冲突值（不同文本的 items 不重叠）
       — 相同文本的 items 在同一 X 位置不算冲突（标签被复制到两行）
    """
    if len(rows) < 2:
        return rows

    i = 0
    while i < len(rows) - 1:
        row_a = rows[i]
        row_b = rows[i + 1]

        items_a = row_a.get("items", [])
        items_b = row_b.get("items", [])
        if not items_a or not items_b:
            i += 1
            continue

        # 检测首列标签是否相同
        texts_a = row_a.get("texts", [])
        texts_b = row_b.get("texts", [])
        label_a = texts_a[0].strip() if texts_a else ""
        label_b = texts_b[0].strip() if texts_b else ""

        same_label = (
            (label_a and label_b and label_a == label_b)
            or (label_a and not label_b)
            or (not label_a and label_b)
        )
        if not same_label:
            i += 1
            continue

        # 检测列冲突：同一 X 位置有不同文本的 items → 是不同行，不合并
        has_conflict = False
        for it_a in items_a:
            xc_a = (it_a.get("x0", 0) + it_a.get("x1", 0)) / 2
            text_a = it_a.get("text", "").strip()
            for it_b in items_b:
                xc_b = (it_b.get("x0", 0) + it_b.get("x1", 0)) / 2
                text_b = it_b.get("text", "").strip()
                if abs(xc_a - xc_b) <= 8.0:
                    # 同一 X 位置的不同文本 → 冲突
                    if text_a != text_b:
                        has_conflict = True
                        break
            if has_conflict:
                break

        if has_conflict:
            i += 1
            continue

        # 合并两行（去重：相同文本只保留一个）
        seen_texts_xc = set()
        merged_items = []
        for it in items_a + items_b:
            xc = round((it.get("x0", 0) + it.get("x1", 0)) / 2, 1)
            t = it.get("text", "").strip()
            key = (xc, t)
            if key not in seen_texts_xc:
                seen_texts_xc.add(key)
                merged_items.append(it)

        merged = _build_row_dict(merged_items)
        rows[i] = merged
        del rows[i + 1]
        # 不递增 i，继续在同一位置检查

    return rows


def _find_row_label(row: List[str]) -> Optional[str]:
    """从 pdf2docx 表格行中提取行标签（第一个非空单元格）。

    Returns:
        行标签文本，或 None（整行都空）
    """
    for cell in row:
        if cell and str(cell).strip():
            return str(cell).strip()
    return None


def _match_row_label_to_liteparse(
    label: str,
    lp_rows: List[dict],
    used_indices: set,
) -> Optional[int]:
    """在 liteparse 行中查找与标签匹配的行。

    匹配策略（由紧到松）：
    1. 精确子串匹配：label 出现在某行的某个 text 中
    2. 归一化子串匹配：label_norm 出现在 norm_text 中
    3. 归一化等值匹配：label_norm 和某个 norm_text 相等

    Returns:
        匹配到的 liteparse 行索引，或 None
    """
    if not label:
        return None

    label_norm = _normalize_for_search(label)

    # 第一遍：精确子串匹配（优先匹配未使用的行）
    for idx, row in enumerate(lp_rows):
        if idx in used_indices:
            continue
        for text in row["texts"]:
            if label in text:
                return idx

    # 第二遍：归一化子串匹配
    for idx, row in enumerate(lp_rows):
        if idx in used_indices:
            continue
        for norm_text in row["norm_texts"]:
            if label_norm and label_norm in norm_text:
                return idx
            if norm_text and norm_text in label_norm:
                return idx

    # 第三遍：归一化等值匹配
    for idx, row in enumerate(lp_rows):
        if idx in used_indices:
            continue
        if label_norm in row["norm_texts"]:
            return idx

    return None


# ============================================================
# 行级存在性校验
# ============================================================

def _build_row_nonempty_set(row: List[str], skip_first: bool = False) -> set:
    """提取一行中所有非空、去空格后的唯一值集合。"""
    start = 1 if skip_first else 0
    result = set()
    for i in range(start, len(row)):
        val = str(row[i]).strip() if row[i] else ""
        if val:
            result.add(val)
    return result


def _build_row_value_map(row: List[str], skip_first: bool = False) -> dict:
    """提取一行中 归一化值 -> 列索引 的映射。"""
    start = 1 if skip_first else 0
    result = {}
    for i in range(start, len(row)):
        val = str(row[i]).strip() if row[i] else ""
        if val:
            result[_normalize_for_search(val)] = i
    return result


def classify_rows_with_liteparse(
    table_data: List[List[str]],
    liteparse_text_items: List[dict],
) -> dict:
    """使用 liteparse 行级数据对 pdf2docx 表格的每一行做存在性校验。

    核心思路：
    - liteparse Y 聚类 = 正确的行划分（真值）
    - 用 liteparse 行来验证 pdf2docx 的每一行是否真实存在

    行分类：
    - real:      在 liteparse 中有对应行，且非空值完全匹配
    - phantom:   标签与相邻行相同、非空值是另一行的严格子集（幽灵行）
    - section:   整行只有 1~2 个非空单元格且像是节标题
    - extra:     在 liteparse 中完全找不到对应行
    - empty:     整行为空（无标签）

    Args:
        table_data: pdf2docx 2D 表格
        liteparse_text_items: [{text, x0, y0, x1, y1}, ...]

    Returns:
        {
            "row_status": {
                pdf_idx: {
                    "status": "real" | "phantom" | "section" | "extra" | "merged",
                    "lp_row_idx": int | None,
                    "lp_texts": [str, ...] | None,
                    "reason": str,
                }
            },
            "missing_rows": [{lp_idx, texts, y_range}, ...],  # liteparse 有但 pdf2docx 无
            "table_start_lp_idx": int | None,   # liteparse 中表格数据起始行
            "table_end_lp_idx": int | None,     # liteparse 中表格数据结束行
        }
    """
    if not liteparse_text_items or not table_data:
        return {"row_status": {}, "missing_rows": [], "table_start_lp_idx": None, "table_end_lp_idx": None}

    # --- 0. 构建 liteparse items ---
    items = []
    for ti in liteparse_text_items:
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

    if not items:
        return {"row_status": {}, "missing_rows": [], "table_start_lp_idx": None, "table_end_lp_idx": None}

    # --- 0.5. 合并被 PDF 引擎拆开的小数后缀 ---
    items = _merge_split_decimals(items)

    # --- 1. liteparse 按 Y 聚类为行 ---
    lp_rows = _cluster_items_by_y(items)

    # --- 2. 行标签匹配 (pdf2docx → liteparse) ---
    # 第一阶段：不加 used_lp 限制，让每个 PDF 行找到所有可能的 LP 匹配
    # 多行匹配同一 LP 行的情况将在子集分析中处理
    row_matches: Dict[int, List[int]] = {}  # pdf_idx -> [lp_idx, ...]

    for r, row in enumerate(table_data):
        if not row:
            continue
        label = _find_row_label(row)
        if not label:
            continue

        candidates = []
        label_norm = _normalize_for_search(label)
        for lp_idx, lp_row in enumerate(lp_rows):
            # 精确定位：先按子串匹配
            for text in lp_row["texts"]:
                if label in text:
                    candidates.append(lp_idx)
                    break
            if lp_idx in candidates:
                continue
            # 归一化后匹配
            for norm_text in lp_row["norm_texts"]:
                if label_norm and (label_norm in norm_text or norm_text in label_norm):
                    candidates.append(lp_idx)
                    break

        if candidates:
            row_matches[r] = candidates

    # --- 3. 按 liteparse 行分组，做子集/重叠分析判定幽灵行 ---
    # lp_idx -> [pdf_idx, ...]（同一 LP 行被哪些 PDF 行匹配）
    lp_to_pdf_rows: Dict[int, List[int]] = {}
    for pdf_r, candidates in row_matches.items():
        for lp_idx in candidates:
            if lp_idx not in lp_to_pdf_rows:
                lp_to_pdf_rows[lp_idx] = []
            lp_to_pdf_rows[lp_idx].append(pdf_r)

    # 解析匹配关系 + 幽灵行判定
    resolved_matches: Dict[int, int] = {}  # pdf_idx -> lp_idx (非幽灵行的正式匹配)
    phantom_rows: set = set()
    used_lp: set = set()

    for lp_idx, pdf_rows in lp_to_pdf_rows.items():
        if len(pdf_rows) == 1:
            # 单匹配：直接建立映射
            pdf_r = pdf_rows[0]
            if lp_idx not in used_lp:
                resolved_matches[pdf_r] = lp_idx
                used_lp.add(lp_idx)
            continue

        # 多匹配 → 子集/重叠分析
        # 提取每行的非空值集合（跳过首列标签）
        row_sets = {}
        for pdf_r in pdf_rows:
            row_sets[pdf_r] = _build_row_nonempty_set(table_data[pdf_r], skip_first=True)

        # 找出非幽灵行（有最多非空值的那行）
        max_vals = -1
        primary_pdf = pdf_rows[0]
        for pdf_r in pdf_rows:
            n = len(row_sets[pdf_r])
            if n > max_vals:
                max_vals = n
                primary_pdf = pdf_r

        # 判定其他行是否为幽灵行
        for pdf_r in pdf_rows:
            if pdf_r == primary_pdf:
                continue
            set_r = row_sets[pdf_r]
            set_primary = row_sets[primary_pdf]

            # 🆕 策略1：严格子集（原逻辑）
            is_strict_subset = (set_r and set_r.issubset(set_primary) and (set_primary - set_r))
            is_primary_subset = (set_primary and set_primary.issubset(set_r) and (set_r - set_primary))

            if is_strict_subset:
                phantom_rows.add(pdf_r)
            elif is_primary_subset:
                phantom_rows.add(primary_pdf)
                primary_pdf = pdf_r
                max_vals = len(set_r)
            else:
                # 🆕 策略2：高重叠度检测（处理缺列导致的列错位）
                # 当一行缺失某列时，值会整体偏移，不再是严格子集
                # 但如果两行共享 >60% 的唯一值，很可能是跨页重复行
                if set_r and set_primary:
                    intersection = set_r & set_primary
                    union = set_r | set_primary
                    if union:
                        overlap_ratio = len(intersection) / len(union)
                        if overlap_ratio >= 0.6:
                            # 高重叠 → 是幽灵行
                            # 非空值少的那个是幽灵行
                            if len(set_r) <= len(set_primary):
                                phantom_rows.add(pdf_r)
                            else:
                                phantom_rows.add(primary_pdf)
                                primary_pdf = pdf_r
                                max_vals = len(set_r)

        # 标记非幽灵行 + 使用 LP 行
        if lp_idx not in used_lp:
            resolved_matches[primary_pdf] = lp_idx
            used_lp.add(lp_idx)

    # --- 4. 生成每行的最终状态 ---
    row_status: Dict[int, dict] = {}

    for r in range(len(table_data)):
        row = table_data[r]
        label = _find_row_label(row)
        if not label:
            row_status[r] = {
                "status": "empty",
                "lp_row_idx": None,
                "lp_texts": None,
                "reason": "整行为空",
            }
            continue

        if r in phantom_rows:
            # 幽灵行
            lp_idx = None
            for lp_i, pdf_rows in lp_to_pdf_rows.items():
                if r in pdf_rows:
                    lp_idx = lp_i
                    break
            row_status[r] = {
                "status": "phantom",
                "lp_row_idx": lp_idx,
                "lp_texts": lp_rows[lp_idx]["texts"] if lp_idx is not None else None,
                "reason": "非空值是相邻同行标签行的严格子集，疑似 pdf2docx 多余行",
            }
        elif r in resolved_matches:
            lp_idx = resolved_matches[r]
            lp_row = lp_rows[lp_idx]

            # 检查是否是非表格行（节标题等）
            non_empty_count = sum(1 for cell in row if cell and str(cell).strip())
            lp_item_count = len(lp_row["texts"])
            if non_empty_count <= 2 and lp_item_count <= 2:
                row_status[r] = {
                    "status": "section",
                    "lp_row_idx": lp_idx,
                    "lp_texts": lp_row["texts"],
                    "reason": "仅 1~2 个单元格，为非表格节标题",
                }
            else:
                # 检查值的一致性
                all_match = True
                pdf_vals = _build_row_value_map(row, skip_first=True)
                lp_norm_set = set(lp_row["norm_texts"])

                for norm_val in pdf_vals:
                    if norm_val not in lp_norm_set:
                        # 还尝试检查子串匹配
                        found = False
                        for lp_norm in lp_norm_set:
                            if norm_val in lp_norm or lp_norm in norm_val:
                                found = True
                                break
                        if not found:
                            all_match = False
                            break

                if all_match:
                    row_status[r] = {
                        "status": "real",
                        "lp_row_idx": lp_idx,
                        "lp_texts": lp_row["texts"],
                        "reason": "非空值全部在 liteparse 对应行中找到",
                    }
                else:
                    row_status[r] = {
                        "status": "real",
                        "lp_row_idx": lp_idx,
                        "lp_texts": lp_row["texts"],
                        "reason": "部分值在 liteparse 中未匹配，可能有少量差异",
                    }
        else:
            # 在 liteparse 中找不到匹配
            row_status[r] = {
                "status": "extra",
                "lp_row_idx": None,
                "lp_texts": None,
                "reason": f"行标签 '{label}' 在 liteparse 中未找到对应行",
            }

    # --- 5. 检测 liteparse 中未被消费的行（缺失行） ---
    consumed_lp = set()
    for status in row_status.values():
        if status.get("lp_row_idx") is not None:
            consumed_lp.add(status["lp_row_idx"])

    # 推断列签名（用于行评分）
    col_schema = infer_column_schema(lp_rows)

    missing_rows = []
    for lp_idx, lp_row in enumerate(lp_rows):
        if lp_idx not in consumed_lp:
            texts = lp_row["texts"]
            # 跳过只有1个短文本的行（很可能是页面标题、段落等）
            if len(texts) <= 1 and all(len(t) < 20 for t in texts):
                continue
            missing_rows.append({
                "lp_idx": lp_idx,
                "texts": texts,
                "y_range": (lp_row["y_min"], lp_row["y_max"]),
            })

    # --- 6. 推断表格数据在 liteparse 中的起止行（列签名分值制） ---
    table_start_lp = None
    table_end_lp = None
    for lp_idx, lp_row in enumerate(lp_rows):
        texts = lp_row["texts"]
        score = _score_table_row(texts, col_schema)

        if score >= 2 and table_start_lp is None:
            table_start_lp = lp_idx
        if score >= 2:
            table_end_lp = lp_idx

    return {
        "row_status": row_status,
        "missing_rows": missing_rows,
        "table_start_lp_idx": table_start_lp,
        "table_end_lp_idx": table_end_lp,
    }


# ============================================================
# 主函数（原有）
# ============================================================

def diff_table_with_liteparse(
    table_data: List[List[str]],
    liteparse_text_items: List[dict],
) -> dict:
    """对 pdf2docx 表格做行/列层级 liteparse 对比。

    对比逻辑（纯相对位置，不依赖跨源绝对坐标）：

    ┌──────────────────────────────────────────────────────────┐
    │ pdf2docx 表格 data                        liteparse items │
    │ ┌──────┬──────┬──────┐              ["生息资产",100,200, │
    │ │生息资产│ 100  │ 200  │               "非生息资产",50,80] │
    │ ├──────┼──────┼──────┤                                    │
    │ │非生息 │ 50   │ 80   │                                    │
    │ └──────┴──────┴──────┘                                    │
    │                                                           │
    │ Step 1: liteparse items 按 Y 聚类 → L_rows                 │
    │         L_row[0]: [生息资产, 100, 200]  (按 X 排序)         │
    │         L_row[1]: [非生息资产, 50, 80]                      │
    │                                                           │
    │ Step 2: pdf2docx row[0] label="生息资产"                   │
    │         → 在 L_rows 文本中匹配 → L_row[0]                   │
    │         pdf2docx row[1] label="非生息"                     │
    │         → 在 L_rows 文本中匹配 → L_row[1]                   │
    │                                                           │
    │ Step 3: 对齐后逐列对比（按索引，不按 X 坐标）                  │
    │         D[0][0]="生息资产" vs L[0][0]="生息资产" → ✅        │
    │         D[0][1]="100"      vs L[0][1]="100"      → ✅       │
    │         D[1][2]="80"       vs L[1][2]="80"       → ✅       │
    │                                                           │
    │ Step 4: 结构差异检测                                          │
    │         D_row 有 label 在 L_rows 中找不到 → 多余行            │
    │         L_row 有 text 未被任何 D_row 消费 → 缺失行             │
    │         D 列数 > L 列数 → 多余列                              │
    │         D 列数 < L 列数 → 缺失列                              │
    └──────────────────────────────────────────────────────────┘

    Args:
        table_data: pdf2docx 提取的 2D 表格 [[cell, ...], ...]
        liteparse_text_items: [{text, x0, y0, x1, y1}, ...]

    Returns:
        dict:
        {
            "cell_diffs": {         # 单元格差异
                "r,c": {
                    "status": "suspicious" | "match",
                    "cell_value": str,
                    "liteparse_value": str,
                    "liteparse_hint": str,
                }
            },
            "extra_rows": [idx, ...],   # pdf2docx 多余行索引
            "missing_row_texts": [...],  # liteparse 中未被覆盖的行文本
            "extra_cols": {r: [c, ...]}, # 每行多余的列索引
            "missing_cols": {r: n},     # 每行缺少的列数
            "unmatched_items": [...],    # liteparse 中完全未被消费的文本项
        }
    """
    if not liteparse_text_items or not table_data:
        return {"cell_diffs": {}}

    # --- 0. 构建 liteparse items (带坐标) ---
    items = []
    for ti in liteparse_text_items:
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

    if not items:
        return {"cell_diffs": {}}

    # 合并被 PDF 引擎拆开的小数后缀
    items = _merge_split_decimals(items)

    # --- 1. 将 liteparse items 按 Y 聚类为逻辑行 ---
    lp_rows = _cluster_items_by_y(items)

    # --- 2. 行对齐：pdf2docx 行 ↔ liteparse 行 ---
    matched_row_pairs: List[Tuple[int, int]] = []  # [(pdf_idx, lp_idx), ...]
    used_lp_indices: set = set()
    extra_rows: List[int] = []

    for r, row in enumerate(table_data):
        if not row:
            continue

        label = _find_row_label(row)
        if not label:
            continue

        lp_idx = _match_row_label_to_liteparse(label, lp_rows, used_lp_indices)
        if lp_idx is not None:
            matched_row_pairs.append((r, lp_idx))
            used_lp_indices.add(lp_idx)
        else:
            # 行标签在 liteparse 中找不到 → 可能是多余行
            extra_rows.append(r)

    # 未被使用的 liteparse 行 → 可能是缺失行
    missing_lp_indices = [
        i for i in range(len(lp_rows)) if i not in used_lp_indices
    ]

    # --- 3. 同行同列对比 ---
    cell_diffs: Dict[str, dict] = {}

    for pdf_r, lp_r in matched_row_pairs:
        pdf_row = table_data[pdf_r]
        lp_row = lp_rows[lp_r]
        lp_texts = lp_row["texts"]
        lp_norm_texts = lp_row["norm_texts"]

        n_pdf_cols = len(pdf_row)
        n_lp_cols = len(lp_texts)

        # 🆕 基于文本相似度的列对齐（而非索引对齐）
        # 为每个 PDF 非空列值找到最佳匹配的 LP 列
        lp_used = set()

        for c in range(n_pdf_cols):
            cell_str = str(pdf_row[c]).strip() if pdf_row[c] else ""
            if not cell_str:
                continue
            cell_norm = _normalize_for_search(cell_str)

            # 尝试在 LP 列中找最佳匹配
            match_type = "none"
            best_lp_idx = -1
            best_lp_str = ""

            # 第一轮：精确匹配（同值）
            for lp_c in range(n_lp_cols):
                if lp_c in lp_used:
                    continue
                if lp_texts[lp_c] == cell_str:
                    match_type = "exact"
                    best_lp_idx = lp_c
                    best_lp_str = lp_texts[lp_c]
                    break

            # 第二轮：归一化匹配
            if match_type == "none":
                for lp_c in range(n_lp_cols):
                    if lp_c in lp_used:
                        continue
                    if cell_norm == lp_norm_texts[lp_c]:
                        match_type = "normalized"
                        best_lp_idx = lp_c
                        best_lp_str = lp_texts[lp_c]
                        break

            # 第三轮：子串匹配
            if match_type == "none":
                for lp_c in range(n_lp_cols):
                    if lp_c in lp_used:
                        continue
                    lp_norm = lp_norm_texts[lp_c]
                    if cell_norm and lp_norm:
                        if cell_norm in lp_norm or lp_norm in cell_norm:
                            match_type = "substring"
                            best_lp_idx = lp_c
                            best_lp_str = lp_texts[lp_c]
                            break

            # 第四轮：数值容错匹配
            if match_type == "none":
                for lp_c in range(n_lp_cols):
                    if lp_c in lp_used:
                        continue
                    lp_norm = lp_norm_texts[lp_c]
                    try:
                        if (cell_norm.replace("-", "").replace(".", "").isdigit() and
                            lp_norm.replace("-", "").replace(".", "").isdigit()):
                            f1 = float(cell_norm)
                            f2 = float(lp_norm)
                            if f1 == f2:
                                match_type = "numeric"
                                best_lp_idx = lp_c
                                best_lp_str = lp_texts[lp_c]
                                break
                    except ValueError:
                        pass

            if match_type != "none" and best_lp_idx >= 0:
                lp_used.add(best_lp_idx)
                continue  # 匹配成功，不需要报告差异

            # 在 LP 中找不到匹配 → 报告可疑
            # 检查是否是 LP 索引位置的值（回退到索引对齐，用于生成报告）
            lp_hint_idx = c if c < n_lp_cols else -1
            if lp_hint_idx >= 0:
                lp_hint_str = lp_texts[lp_hint_idx]
            else:
                lp_hint_str = "(无对应列)"

            hint_parts = [
                f"pdf2docx: {cell_str}",
                f"liteparse: {lp_hint_str}",
                f"位置: 行{pdf_r}, 列{c}",
            ]

            cell_diffs[f"{pdf_r},{c}"] = {
                "status": "suspicious",
                "cell_value": cell_str,
                "liteparse_value": lp_hint_str,
                "liteparse_hint": "\n".join(hint_parts),
            }

    # --- 4. 结构差异检测 ---

    # 多余行：已在上面检测
    # extra_rows 保持原样

    # 缺失行：liteparse 中有但 pdf2docx 没有的行
    missing_row_texts = []
    for idx in missing_lp_indices:
        missing_row_texts.append({
            "liteparse_row_idx": idx,
            "texts": lp_rows[idx]["texts"],
            "y_range": (lp_rows[idx]["y_min"], lp_rows[idx]["y_max"]),
        })

    # 多余的列 / 缺失的列：基于匹配行对
    extra_cols: Dict[int, list] = {}
    missing_cols: Dict[int, int] = {}

    for pdf_r, lp_r in matched_row_pairs:
        pdf_row = table_data[pdf_r]
        lp_col_count = len(lp_rows[lp_r]["texts"])

        if len(pdf_row) > lp_col_count:
            extra = list(range(lp_col_count, len(pdf_row)))
            # 检查多余的列是否全是空值
            real_extra = [c for c in extra if pdf_row[c] and str(pdf_row[c]).strip()]
            if real_extra:
                extra_cols[pdf_r] = real_extra
        elif len(pdf_row) < lp_col_count:
            missing_cols[pdf_r] = lp_col_count - len(pdf_row)

    # 完全未被任何 pdf2docx cell 消费的 liteparse items
    # 包括缺失行中的所有 items
    unmatched_items = []
    for idx in missing_lp_indices:
        for item in lp_rows[idx]["items"]:
            unmatched_items.append({
                "text": item["text"],
                "x": item["x0"],
                "y": item["y0"],
                "liteparse_row_idx": idx,
                "reason": "所在行未被 pdf2docx 捕获",
            })

    return {
        "cell_diffs": cell_diffs,
        "extra_rows": extra_rows,
        "missing_row_texts": missing_row_texts,
        "extra_cols": extra_cols,
        "missing_cols": missing_cols,
        "unmatched_items": unmatched_items,
    }
