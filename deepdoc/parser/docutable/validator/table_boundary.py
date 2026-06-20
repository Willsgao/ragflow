# -*- coding: utf-8 -*-
"""
表格边界检测器 — 利用 liteparse table_regions 限定表格文本范围 & 检测同页拆分

核心能力：
1. match_tables_to_regions()     — pdf2docx 表格 ↔ liteparse region 匹配
2. scope_text_items_to_region()  — 从全页 text_items 中只取指定区域内的文本
3. detect_adjacent_splits()      — 基于 liteparse 检测相邻表拆分（同页+跨页，供合并决策）

设计原则：
- 所有操作零 API 成本
- liteparse 缓存不可用时降级为全页 text_items
- 匹配失败不阻塞主流程
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


# ================================================================
# 1. 表格 ↔ liteparse region 匹配
# ================================================================

def match_tables_to_regions(
    tables: List[dict],
    regions: List[dict],
) -> Dict[int, int]:
    """将同一页上的 pdf2docx 表格匹配到 liteparse 的 table_regions。

    匹配策略（按优先级）：
    1. 按 Y 坐标从上到下排序匹配：liteparse region y0 对应 pdf2docx 出现顺序
    2. 用表格首列标签在 region_text 中搜索做交叉验证

    Args:
        tables: 同一页的 pdf2docx 表格列表，每个含 "data" 字段
        regions: liteparse TableRegion.to_dict() 列表，每个含 y0/y1/region_text

    Returns:
        {table_index_in_tables: region_index_in_regions}
        key 为 tables 列表中的本地索引（0-based）
    """
    if not regions:
        return {}

    # 按 Y 坐标从上到下排序 regions
    sorted_regions = sorted(enumerate(regions), key=lambda x: x[1].get("y0", 0))

    # 按 tables 列表顺序（假设已按出现顺序排列）
    matching = {}

    # 场景1: tables 和 regions 数量一致 → 1:1 按顺序匹配
    if len(tables) == len(sorted_regions):
        for table_idx, (region_idx, _) in enumerate(sorted_regions):
            matching[table_idx] = region_idx
        return _verify_matching(matching, tables, regions)

    # 场景2: 数量不一致 → 用内容交叉验证
    for table_idx, table in enumerate(tables):
        best_region = _find_best_region(table, regions)
        if best_region is not None:
            matching[table_idx] = best_region

    return matching


def _find_best_region(table: dict, regions: List[dict]) -> Optional[int]:
    """用表格首列标签在 region_text 中搜索，找到最佳匹配的 region。"""
    data = table.get("data", [])
    if not data or not regions:
        return None

    # 提取表格首列的文本标签（取前 3 行做特征）
    labels = []
    for row in data[:3]:
        if row and row[0]:
            labels.append(str(row[0]).strip())

    if not labels:
        return None

    scores = []
    for idx, region in enumerate(regions):
        region_text = region.get("region_text", "")
        if not region_text:
            continue
        score = sum(1 for lbl in labels if lbl and lbl in region_text)
        scores.append((score, idx))

    if not scores:
        return None

    scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_idx = scores[0]

    if best_score > 0:
        return best_idx
    return None


def _verify_matching(
    matching: Dict[int, int],
    tables: List[dict],
    regions: List[dict],
) -> Dict[int, int]:
    """交叉验证匹配结果，纠正明显的错配。"""
    verified = {}
    for table_idx, region_idx in matching.items():
        if region_idx >= len(regions):
            continue
        labels = _get_table_labels(tables[table_idx])
        region_text = regions[region_idx].get("region_text", "")
        if labels and region_text:
            match_count = sum(1 for lbl in labels if lbl and lbl in region_text)
            if match_count == 0:
                # 完全没匹配 → 尝试重新匹配
                new_idx = _find_best_region(tables[table_idx], regions)
                if new_idx is not None and new_idx != region_idx:
                    verified[table_idx] = new_idx
                    continue
        verified[table_idx] = region_idx
    return verified


def _get_table_labels(table: dict) -> List[str]:
    """获取表格前几行的首列标签。"""
    data = table.get("data", [])
    return [str(row[0]).strip() for row in data[:5] if row and row[0]]


# ================================================================
# 2. 文本范围限定
# ================================================================

def scope_text_items_to_region(
    text_items: List[dict],
    region: dict,
    context_margin: float = 30.0,
) -> List[dict]:
    """从全页 text_items 中只保留中心点落在指定 region bbox 内的文本项。

    Args:
        text_items: liteparse 该页的全部 text_items (dict 格式，含 x0/y0/x1/y1)
        region:    liteparse TableRegion.to_dict()，含 x0/y0/x1/y1
        context_margin: Y 方向扩展量(pt)，上方扩展以捕获表格标题

    Returns:
        范围限定后的 text_items 列表（引用原对象，不拷贝）
    """
    if not text_items or not region:
        return text_items

    rx0 = region.get("x0", 0)
    rx1 = region.get("x1", float("inf"))
    ry0 = region.get("y0", 0) - context_margin  # 上方扩展
    ry1 = region.get("y1", float("inf"))

    scoped = []
    for ti in text_items:
        x0 = ti.get("x0", 0)
        y0 = ti.get("y0", 0)
        x1 = ti.get("x1", 0)
        y1 = ti.get("y1", 0)

        if x1 <= x0 or y1 <= y0:
            continue

        # 中心点判断
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2

        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            scoped.append(ti)

    # 如果限定后为空，回退为全部
    return scoped if scoped else text_items


def get_scoped_items_for_table(
    table: dict,
    all_tables_on_page: List[dict],
    liteparse_page: dict,
    context_margin: float = 30.0,
) -> List[dict]:
    """为单个表格获取范围限定后的 liteparse text_items。

    如果同页只有一个表格，返回全页 text_items。
    如果有多个表格且有 liteparse regions，匹配后用 scoped items。

    Args:
        table:                目标表格
        all_tables_on_page:   同页所有表格列表
        liteparse_page:       liteparse 该页的 PageResult.to_dict()
        context_margin:       Y 方向扩展量

    Returns:
        text_items 列表（dict 格式）
    """
    text_items = liteparse_page.get("text_items", [])
    if not text_items:
        return []

    # 同页单表 → 全页 items
    if len(all_tables_on_page) <= 1:
        return text_items

    regions = liteparse_page.get("table_regions", [])
    if not regions:
        return text_items

    # 找到当前 table 在 all_tables_on_page 中的索引
    try:
        table_idx = all_tables_on_page.index(table)
    except ValueError:
        return text_items

    matching = match_tables_to_regions(all_tables_on_page, regions)
    if table_idx not in matching:
        return text_items

    region_idx = matching[table_idx]
    if region_idx >= len(regions):
        return text_items

    return scope_text_items_to_region(text_items, regions[region_idx], context_margin)


# ================================================================
# 3. 相邻表拆分检测（同页 + 跨页）
# ================================================================

def detect_adjacent_splits(
    tables: List[dict],
    liteparse_data: dict,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int, str]]]:
    """检测相邻表格（同页或跨页）是否应合并。

    分三层判断（优先级从高到低）：
    1. liteparse 同页判定：同页多表，liteparse 只有 1 个 region → 高置信拆分
    2. 跨页特征检测：前一页最后一张表 + 后一页第一张表，列数一致 + 后表无表头
    3. 纯启发式：列数相同、后表无表头、中间无实质文本阻断

    Args:
        tables:         全部表格列表（已按页码排序）
        liteparse_data: liteparse ParseResult.to_dict()

    Returns:
        (confident_merges, suggested_merges)
        - confident_merges: [(i, j), ...]  高置信 → 自动合并
        - suggested_merges: [(i, j, reason), ...]  建议合并
    """
    confident = []
    suggested = []

    if not tables:
        return confident, suggested

    # 收集有效表格的相邻对
    valid_indices = [i for i, t in enumerate(tables) if t.get("data")]
    if len(valid_indices) < 2:
        return confident, suggested

    # 预处理：liteparse 同页分组
    same_page_info = _build_page_info(tables, liteparse_data)

    for idx_pos in range(len(valid_indices) - 1):
        i = valid_indices[idx_pos]
        j = valid_indices[idx_pos + 1]
        table_a = tables[i]
        table_b = tables[j]
        page_a = table_a.get("page", 0)
        page_b = table_b.get("page", 0)

        # 先尝试 liteparse 辅助判断（同页多表场景）
        if page_a == page_b and page_a in same_page_info:
            result = _check_with_liteparse(table_a, table_b, same_page_info[page_a])
        else:
            # 跨页或单页无 liteparse → 启发式判断
            result = _check_with_heuristic(table_a, table_b, tables, valid_indices, i, j)

        if result == "confident":
            confident.append((i, j))
        elif result:
            suggested.append((i, j, result))

    return confident, suggested


def _build_page_info(tables, liteparse_data):
    """构建每页的 liteparse 摘要信息。

    Returns:
        {page_num: {"regions": [...], "table_count": N}}  仅包含多表页
    """
    info = {}
    if not liteparse_data:
        return info

    page_table_counts: Dict[int, int] = {}
    for t in tables:
        if t.get("data"):
            page_table_counts.setdefault(t.get("page", 0), 0)
            page_table_counts[t.get("page", 0)] += 1

    for page_num, count in page_table_counts.items():
        if count < 2:
            continue
        lp_page = _get_liteparse_page(liteparse_data, page_num)
        if lp_page:
            info[page_num] = {
                "regions": lp_page.get("table_regions", []),
                "table_count": count,
            }

    return info


def _check_with_liteparse(table_a, table_b, page_info):
    """基于 liteparse region 判断两表是否应合并。

    Returns: "confident" | "same_region_no_header" | "col_diff" | ""
    """
    regions = page_info.get("regions", [])
    table_count = page_info.get("table_count", 2)

    # 规则：liteparse 只有 1 个 region，pdf2docx 有多个表 → 大概率拆分
    if len(regions) == 1 and table_count >= 2:
        return _check_split_pattern(table_a, table_b)

    # liteparse 也有多个 region → 检查首列标签是否在同一个 region 中
    if len(regions) >= 2:
        labels_a = _get_table_labels(table_a)
        labels_b = _get_table_labels(table_b)
        for region in regions:
            rt = region.get("region_text", "")
            if rt:
                in_a = any(lbl and lbl in rt for lbl in labels_a)
                in_b = any(lbl and lbl in rt for lbl in labels_b)
                if in_a and in_b:
                    # 两个表的首列标签都在同一个 region → 可能拆分
                    return _check_split_pattern(table_a, table_b)

    return ""


def _check_with_heuristic(table_a, table_b, all_tables, valid_indices, idx_a, idx_b):
    """启发式判断跨页/无 liteparse 的两表是否应合并。

    Returns: "confident" | "cross_page_match" | "col_diff" | ""
    """
    import re

    page_a = table_a.get("page", 0)
    page_b = table_b.get("page", 0)

    # 列数检查
    cols_a = max((len(r) for r in table_a.get("data", [])), default=0)
    cols_b = max((len(r) for r in table_b.get("data", [])), default=0)
    if cols_a == 0 or cols_b == 0:
        return ""

    col_match = (cols_a == cols_b)
    col_close = abs(cols_a - cols_b) <= 2

    if not col_match and not col_close:
        return ""

    # 后表首行无表头
    has_header = _table_has_header(table_b)

    # 中间有实质文本阻断？
    ctx_b = table_b.get("context_text", "")
    meaningful_lines = []
    if ctx_b:
        meaningful_lines = [l for l in ctx_b.split('\n')
                           if l.strip() and not re.match(r'^[\d\-\s]+$', l.strip())]

    # 规则：后表是本页第一个且有实质描述文字 → 不合并
    if _is_first_on_page(idx_b, all_tables, valid_indices) and len(meaningful_lines) >= 1:
        return ""

    # 规则：有 2 行以上实质文本 → 不合并
    if len(meaningful_lines) >= 2:
        return ""

    # 跨页额外检查
    if page_a != page_b:
        # 跨页：页码必须连续
        if page_b != page_a + 1:
            return ""
        # 表 A 必须是以数据行结尾（不是以合计或标题结尾 → 说明表格没完）
        if not _table_ends_with_data(table_a):
            return ""
    else:
        # 同页：不用额外检查
        pass

    if col_match:
        if not has_header:
            return "confident"
        else:
            return "same_cols_with_header"
    else:
        if not has_header:
            return "col_diff"
        return ""


def _table_has_header(table):
    """后表首行是否包含表头（文字标签）。"""
    data = table.get("data", [])
    if not data:
        return False
    first_row = data[0]
    for cell in first_row:
        text = str(cell).strip()
        if text and not _is_numeric_cell(text):
            return True
    return False


def _table_ends_with_data(table):
    """判断表格最后一行是否为数据行（而非合计/标题等终结行）。"""
    data = table.get("data", [])
    if len(data) < 2:
        return False
    last_row = data[-1]
    last_label = str(last_row[0]).strip() if last_row else ""
    # 合计行关键词 → 表格逻辑终结
    summary_kw = ["合计", "总计", "总额", "小计"]
    if any(kw in last_label for kw in summary_kw):
        return False
    # 有数值特征 → 数据行
    for cell in last_row[1:]:
        if _is_numeric_cell(str(cell)):
            return True
    return False


def _is_first_on_page(idx, all_tables, valid_indices):
    """判断表格是否为本页第一个有效表格。"""
    page = all_tables[idx].get("page", 0)
    for k in valid_indices:
        if k == idx:
            break
        if all_tables[k].get("page") == page and all_tables[k].get("data"):
            return False
    return True


def _check_split_pattern(table_a: dict, table_b: dict) -> str:
    """检查两表是否是被拆分的同一个表格。

    Returns:
        "confident" — 高置信（列数相同 + 后表无表头）
        "same_cols_no_header" — 列数相同但不确定
        "col_diff" — 列数略有差异
        "" — 不应合并
    """
    data_a = table_a.get("data", [])
    data_b = table_b.get("data", [])

    if not data_a or not data_b:
        return ""

    cols_a = max(len(r) for r in data_a) if data_a else 0
    cols_b = max(len(r) for r in data_b) if data_b else 0

    if cols_a == 0 or cols_b == 0:
        return ""

    # 检查后表首行是否为表头（含文字标签）
    first_row_b = data_b[0] if data_b else []
    has_header = False
    for cell in first_row_b:
        text = str(cell).strip()
        if text and not _is_numeric_cell(text):
            has_header = True
            break

    if cols_a == cols_b:
        if not has_header:
            return "confident"
        else:
            return "same_cols_no_header"

    # 列数差异 ≤2 → 可能合并
    col_diff = abs(cols_a - cols_b)
    if col_diff <= 2 and not has_header:
        return "col_diff"

    return ""


def _is_numeric_cell(text: str) -> bool:
    """判断单元格是否为纯数值。"""
    import re
    text = str(text).strip()
    if not text:
        return True  # 空值也视为"无表头特征"
    return bool(re.match(r'^[\d,.\-()（）%]+$', text))


# ================================================================
# 4. 辅助：liteparse 页面查询
# ================================================================

def _get_liteparse_page(liteparse_data: dict, page_num: int) -> Optional[dict]:
    """从 liteparse ParseResult.to_dict() 获取指定页。"""
    pages = liteparse_data.get("pages", [])
    for p in pages:
        if p.get("page_number") == page_num:
            return p
    return None
