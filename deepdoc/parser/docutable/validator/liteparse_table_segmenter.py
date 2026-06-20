# -*- coding: utf-8 -*-
"""
liteparse 表格区域分割器（纯规则驱动，无 LLM 依赖）

这是双源融合架构的第一步：
  仅使用 liteparse 自身的数据结构（table_regions + text_items），
  以纯规则方式将每一页的文本精确切分为独立的逻辑表格。

核心能力：
  1. 基于 liteparse 内置 table_regions 的区域切分（bbox 过滤 text_items）
  2. text_items → Y聚类 → 逻辑行（复用 cell_differ 的聚类逻辑）
  3. 跨页续表启发式拼接（列结构匹配 + 无表头检测）
  4. 覆盖度验证报告（孤儿 text_items 检测、表数统计）

设计原则：
  - 零 API 成本，零 LLM 依赖
  - 完全基于 liteparse 数据，与 pdf2docx 完全解耦
  - 可独立运行，也可作为后续 LLM 增强的基线
  - region 不可用时降级为全页 text_items

输出：
  tables: [
    {
      "table_id": 0,                # 全局唯一 ID（分割后）
      "pages": [8],                 # 所属页码（跨页合并后可能多页）
      "page": 8,                    # 起始页（向前兼容）
      "y0": 100.5, "y1": 500.3,     # liteparse 坐标系 Y 范围
      "text_items": [...],          # 该表的 liteparse text_items（已按Y,X排序）
      "rows": [...],                # Y 聚类后的行列表
      "row_count": 15,              # 行数
      "is_cross_page": False,       # 是否跨页
      "caption": "财务摘要",         # 从 region.context_text 提取
      "region_index": 0,            # 来源 region 索引（-1 表示无 region）
      "confidence": 0.85,           # 来源 region 置信度
      "column_x_ranges": [(50,200), ...],  # 列 X 范围（从所有行统计）
    },
    ...
  ]

  report: {
    "total_pages": 20,
    "table_pages": 5,
    "total_tables": 7,
    "cross_page_merges": 1,
    "orphan_page_items": {          # 表格页上未被归属的 items
      8: [{"text": "...", "y0": ..., "y1": ...}, ...],
    },
    "page_details": {
      8: {"tables": 2, "items_total": 45, "items_assigned": 43, "orphans": 2},
    },
    "table_summaries": [...],
  }
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 复用 cell_differ 的核心工具（Y 聚类、文本归一化、列签名、小数合并）
from .cell_differ import (
    _cluster_items_by_y,
    _normalize_for_search,
    infer_column_schema,
    classify_item_type,
    _score_table_row,
    _merge_split_decimals,
    _build_row_dict,
)


# ================================================================
# 0.1. 表格重构专用日志
# ================================================================
_LOG_INITIALIZED = False
_LOG = None


def _setup_table_logger() -> logging.Logger:
    """初始化表格重构专用日志，写入 data/mid_cache/logs/ 目录。

    日志记录关键决策点（列数估计、稀疏列合并、混合表拆分等），
    便于排查表格切分、列对齐异常等问题。
    """
    global _LOG_INITIALIZED, _LOG
    if _LOG_INITIALIZED and _LOG is not None:
        return _LOG

    # 找项目根目录
    log_dir = Path(__file__).resolve().parent.parent.parent / "data" / "mid_cache" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 每次运行生成带时间戳的日志文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"table_reconstruction_{timestamp}.log"

    logger = logging.getLogger("table_reconstructor")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # 不传播到 root logger

    # 清除已有的 handler（防止重复初始化）
    logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # 同时输出到控制台（INFO 级别以上）
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    _LOG = logger
    _LOG_INITIALIZED = True
    _LOG.info("=" * 60)
    _LOG.info("表格重构日志启动 — 日志文件: %s", log_path)
    _LOG.info("=" * 60)
    return _LOG


def _get_log() -> logging.Logger:
    """获取表格重构专用日志实例。"""
    global _LOG
    if _LOG is None:
        _setup_table_logger()
    return _LOG


# ================================================================
# 0. 模块级常量（统一维护，避免多处硬编码不一致）
# ================================================================

# 财务表头关键词（多处使用：_is_header_like, _find_first_table_header_or_data_row,
# _classify_table_quality, _reclassify_single_table 等）
HEADER_KW = [
    # 报表结构
    '项目', '指标', '科目',
    # 资产/负债/权益
    '资产', '负债', '收入', '支出', '金额', '余额',
    # 比率/占比
    '占比', '比重', '数量', '比例',
    # 债/证券
    '利率', '年利率', '利息',
    # 实体/合并
    '集团', '本行', '本公司', '母公司', '子公司', '合并',
    # 手续费/佣金
    '手续费', '佣金',
    # 损益/净值
    '净值', '市值', '利润', '总额',
    # 阶段/风险
    '阶段', '风险',
    # 净利润/损失
    '净收入', '损失',
    # 时间相关
    '期末', '期初', '年末', '年初',
]

# 变化/增减关键词
DELTA_KW = ['变化', '增减', '变动', '增幅', '±', '增速']

# 实体标识关键词（用于表头完整性检测 — 精确单元格匹配）
ENTITY_KW = ['本集团', '本行', '本公司', '母公司', '子公司']


def _is_dual_entity_header_row(non_empty: List[str]) -> bool:
    """同一行中同时出现 2+ 个实体标签 → 确定性表头行。

    银行/企业年报特有模式：一行的不同列分别标注
    "本集团"和"本行"、"合并"和"母公司"等，用于标记各列归属。
    如："已发行债务证券 | 本集团 | 本行"

    ENTITY_KW 中任意 2 个及以上关键词同时出现即判定为双实体表头行。
    """
    matched = [kw for kw in ENTITY_KW if any(kw in t for t in non_empty)]
    return len(matched) >= 2

# 汇总行关键词
SUMMARY_KW = ['合计', '总计', '总额', '小计', '累计', '平均']

# 单位词
UNIT_KW = ['万元', '千元', '百万元', '亿元', '元', '美元', '港币', '人民币']

# 年份正则
YEAR_PATTERN = re.compile(r'(19|20)\d{2}')

# 日期正则（用于排除日期格式的数值误判）
DATE_PATTERN = re.compile(
    r'\d{1,2}[/-]\d{1,2}[/-](19|20)\d{2}'   # 18/11/2014
    r'|\d{1,2}月\d{1,2}日'                    # 12月31日
    r'|\d{4}[/-]\d{1,2}[/-]\d{1,2}'           # 2014/11/18
)

# 划线占位符（表格中表示"无数据/不适用"）
DASH_ONLY = re.compile(r'^[\s\-–—]+$')

# 纯年号（如 "2024年""2023年"）
YEAR_ONLY = re.compile(r'^[\s　]*(19|20)\d{2}年[\s　]*$')


# ================================================================
# 1. 公开接口
# ================================================================

def segment_tables_from_liteparse(
    liteparse_data: dict,
    enable_cross_page: bool = True,
    region_confidence_threshold: float = 0.3,
) -> Tuple[List[dict], dict]:
    """从 liteparse 数据中分割出所有逻辑表格。

    Args:
        liteparse_data: ParseResult.to_dict()
        enable_cross_page: 是否启用跨页续表自动拼接
        region_confidence_threshold: table_region 最低置信度，
                                    低于此值的 region 被忽略

    Returns:
        (tables, report) — 见模块 docstring
    """
    pages = liteparse_data.get("pages", [])
    if not pages:
        return [], {"error": "liteparse_data 中无页面数据"}

    LOG = _get_log()
    total_items = sum(len(p.get("text_items", [])) for p in pages)
    LOG.info("[流程] segment_tables_from_liteparse 开始: pages=%d items=%d",
             len(pages), total_items)

    # ---- Phase 1: 逐页分割 ----
    all_tables = []  # [(page_num, table_dict), ...] 保持页面顺序

    for lp_page in pages:
        page_num = lp_page.get("page_number", 0)
        text_items_raw = lp_page.get("text_items", [])

        if not text_items_raw:
            continue

        # 标准化 items
        items = _build_items(text_items_raw)

        regions = lp_page.get("table_regions", [])
        is_table_page = lp_page.get("is_table_page", False)

        if regions:
            # 有 region → 用 bbox 切分
            page_tables = _segment_by_regions(
                page_num, items, regions, region_confidence_threshold
            )
        elif is_table_page:
            # 无 region 但标记为表格页 → 全页视为一张表
            page_tables = [_build_single_table(page_num, items, 0)]
        else:
            # 规则 fallback：即使 liteparse 没识别，也扫描 text_items 找内嵌小表
            page_tables = _detect_tables_from_text_clusters(page_num, items)
            if not page_tables:
                continue  # 确认无表格，跳过

        for t in page_tables:
            all_tables.append((page_num, t))

    if not all_tables:
        return [], {"error": "未检测到任何表格"}

    # ---- Phase 1.5: 图表误判过滤 ----
    all_tables, chart_filtered = _filter_chart_like_tables(all_tables)
    if chart_filtered:
        print(f"  [图表过滤] 移除了 {chart_filtered} 个误判为表格的图表标签区域")

    if not all_tables:
        return [], {"error": "所有候选表均为图表误判，无有效表格"}

    # ---- Phase 2: 跨页续表拼接 ----
    cross_page_merges = 0
    if enable_cross_page:
        all_tables, cross_page_merges = _merge_cross_page_tables(all_tables)

    LOG.info("[流程] Phase 2 跨页拼接完成: tables=%d, 合并=%d",
             len(all_tables), cross_page_merges)

    # ---- Phase 3: 全局编号 + 报告生成 ----
    tables = []
    for idx, (_, t) in enumerate(all_tables):
        t["table_id"] = idx
        tables.append(t)

    # ---- Phase 4: 质量优化后处理（纯规则） ----
    # (1) 非财务表格标记
    tables = _filter_non_financial_tables(tables)
    # (2) 多表混合检测与拆分
    tables, split_count = _detect_and_split_mixed_tables(tables)
    LOG.info("[流程] Phase 4 混合表拆分: tables=%d, split_count=%d",
             len(tables), split_count)
    if split_count:
        print(f"  [混合表拆分] 拆分了 {split_count} 张混合表")
    # (3) 相邻相关表格合并（同页续表）
    tables, adj_merge_count = _merge_adjacent_related_tables(tables)
    if adj_merge_count:
        print(f"  [相邻合并] 合并了 {adj_merge_count} 对相邻相关表格")
    # (4) 描述文本边界增强
    tables = _enhance_with_caption_boundary(tables)
    # (5) 完整表格识别 + 分类标记
    tables = _classify_table_quality(tables)
    # (6) 表格边界精细化处理（去尾、去头、跨页检测）
    tables = _refine_table_boundaries(tables)
    # (6.5) 表头完整性检查与恢复
    tables = _recover_missing_headers(tables)
    # (7) 财务表格置信度评分
    tables = _compute_financial_confidence(tables)

    # Phase 4 可能增减表格 → 重新编号
    for i, t in enumerate(tables):
        t["table_id"] = i

    report = _generate_report(tables, liteparse_data, cross_page_merges)

    LOG.info(
        "[流程] segment_tables_from_liteparse 完成: final_tables=%d, "
        "cross_page=%d, split=%d, adj_merge=%d",
        len(tables), cross_page_merges, split_count, adj_merge_count,
    )

    return tables, report


# ================================================================
# 2. 逐页 Region 切分
# ================================================================

def _segment_by_regions(
    page_num: int,
    items: List[dict],
    regions: List[dict],
    confidence_threshold: float,
) -> List[dict]:
    """用 table_regions 的 bbox 将一页的 items 切分为多个表格。

    P1 优化：检测混合 region（图表标签 + 真实表格上下排列），按 Y 大间隙拆分。
    """
    tables = []
    assigned_ids = set()  # 已归属 item 的 id()

    for ri, region in enumerate(regions):
        conf = region.get("confidence", 0.5)
        if conf < confidence_threshold:
            continue

        rx0 = region.get("x0", 0)
        ry0 = region.get("y0", 0)
        rx1 = region.get("x1", float("inf"))
        ry1 = region.get("y1", float("inf"))
        if rx1 <= rx0 or ry1 <= ry0:
            continue

        # 收集区域内 items（中心点判断）
        scoped = []
        for it in items:
            cx = (it["x0"] + it["x1"]) / 2
            cy = it["y_mid"]
            if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
                scoped.append(it)
                assigned_ids.add(id(it))

        # 扩展：收集 region 下方的脚注延续文本（y 超出 ry1 但在合理 margin 内）
        fn_margin = 80.0  # region 下方 80pt 范围内
        fn_items = []
        for it in items:
            if id(it) in assigned_ids:
                continue
            cx = (it["x0"] + it["x1"]) / 2
            cy = it["y_mid"]
            if rx0 <= cx <= rx1 and ry1 < cy <= ry1 + fn_margin:
                fn_items.append(it)
                assigned_ids.add(id(it))

        if fn_items:
            scoped.extend(fn_items)

        if not scoped:
            continue

        # P1: 检测混合 region，按 Y 大间隙拆分为多个子区段
        sub_segments = _split_by_y_gaps(scoped)

        if len(sub_segments) > 1:
            # 有 Y 大间隙 → 逐段处理
            for seg_idx, seg_items in enumerate(sub_segments):
                _build_table_from_items(
                    page_num, seg_items, region, ri, conf,
                    seg_idx, tables, items, assigned_ids
                )
        else:
            # 单段 → 按原逻辑处理
            _build_table_from_items(
                page_num, scoped, region, ri, conf,
                0, tables, items, assigned_ids
            )

    # 处理孤儿 items（不在任何 region 内的）
    orphans = [it for it in items if id(it) not in assigned_ids]

    if orphans and _is_likely_table_content(orphans):
        # 如果孤儿 items 看起来像表格内容（有数值+标签），创建补充表
        rows = _cluster_items_by_y(orphans)
        rows = _normalize_rows_to_columns(rows)
        col_ranges = _estimate_column_x_ranges(rows)
        col_schema = infer_column_schema(rows)
        tables.append({
            "page": page_num,
            "pages": [page_num],
            "y0": min(it["y0"] for it in orphans),
            "y1": max(it["y1"] for it in orphans),
            "text_items": orphans,
            "rows": rows,
            "row_count": len(rows),
            "is_cross_page": False,
            "caption": "",
            "region_index": -1,
            "confidence": 0.0,
            "column_x_ranges": col_ranges,
            "column_schema": col_schema,
        })

    return tables


# ---- P1: Y 大间隙检测与拆分子段 ----

def _split_by_y_gaps(items: List[dict]) -> List[List[dict]]:
    """检测 items 在 Y 方向的大间隙，拆分为多个子区段。

    策略：
    1. 按 Y 排序 items
    2. 计算相邻 items 的 Y 间距
    3. 仅在极端大间隙（> 平均间距的 10 倍 且 > 80pt）处断开
    4. 检验拆分点两侧 items 的 X 列结构：如果列结构相近 → 是同一张表，不拆
    5. 如果第二段的 items 看起来像上半段的续行（共享列 X 位置）→ 不拆

    设计目标：只拆分图表→表格这种页面级大间隙，不拆分 superscript 脚注导致的微小 Y 偏移。
    """
    min_items_for_split = 6  # 至少 6 个 item 才考虑拆分
    if len(items) < min_items_for_split:
        return [items]

    # 按 Y 排序
    sorted_items = sorted(items, key=lambda it: it["y_mid"])

    # 计算 item 间 Y 间距（每个 item 的 y_mid 间距）
    y_mids = [it["y_mid"] for it in sorted_items]
    gaps = [y_mids[i + 1] - y_mids[i] for i in range(len(y_mids) - 1)]
    gaps = [g for g in gaps if g > 0]

    if len(gaps) < 3:
        return [items]

    # 计算平均间距
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap <= 0:
        return [items]

    # 极端大间隙阈值：平均间距 × 10 且至少 80pt
    # 这样的间隙通常意味着页面布局的显著变化（如图表区→表格区）
    big_gap_threshold = max(mean_gap * 10.0, 80.0)

    # 找极端大间隙位置
    split_indices = []
    for i, gap in enumerate(gaps):
        if gap >= big_gap_threshold:
            split_indices.append(i + 1)

    if not split_indices:
        return [items]

    # 构建 candidate segments 并验证是否属于同一张表
    valid_splits = []
    prev = 0
    for si in split_indices:
        seg_size = si - prev
        if seg_size < 2:
            prev = si
            continue

        # === X 列一致性校验 ===
        # 如果两段共享相似的 X 列结构（同一列），则属同一张表，不拆分
        seg_a = sorted_items[prev:si]
        seg_b = sorted_items[si:]

        if len(seg_b) < 2:
            prev = si
            continue

        if _share_column_structure(seg_a, seg_b):
            prev = si
            continue  # 共享列结构 → 同一表，跳过此拆分点

        valid_splits.append(si)
        prev = si

    if not valid_splits:
        return [items]

    # 执行拆分
    segments = []
    prev = 0
    for si in valid_splits:
        segments.append(sorted_items[prev:si])
        prev = si
    if prev < len(sorted_items):
        segments.append(sorted_items[prev:])

    # 如果只有 1 段 → 无有效拆分
    if len(segments) <= 1:
        return [items]

    return segments


def _share_column_structure(seg_a: List[dict], seg_b: List[dict]) -> bool:
    """判断两个 item 段是否共享同样的列 X 结构（属于同一张表）。

    方法：比较两段所有 item 的 X 中心点分布。
    如果 B 段大部分 item 的 xc 落在 A 段 item 的 X 范围内 → 同一张表。
    """
    if not seg_a or not seg_b:
        return True  # 空段不拆分

    # 收集 A 段 item 的 X 中心点
    xc_a = [(it["x0"] + it["x1"]) / 2 for it in seg_a]
    x_min, x_max = min(xc_a), max(xc_a)

    # 统计 B 段 item 中，X 中心点落在 A 段 X 范围内的比例
    if x_max <= x_min:
        return False

    xc_b = [(it["x0"] + it["x1"]) / 2 for it in seg_b]
    in_range = sum(1 for xc in xc_b if x_min - 20 <= xc <= x_max + 20)
    ratio = in_range / len(xc_b)

    # 如果 > 70% 的 B 段 item 与 A 段共享 X 范围 → 同一张表
    return ratio >= 0.70


def _build_table_from_items(
    page_num: int,
    scoped: List[dict],
    region: dict,
    ri: int,
    conf: float,
    seg_idx: int,
    tables: List[dict],
    all_page_items: List[dict],
    assigned_ids: set,
):
    """从一组 items 构建单张表格并加入 tables 列表。

    支持：
    - 首次段(seg_idx==0) 可捕获 caption
    - 非首次段不捕获 caption（因 caption 属于首发段）
    """
    # 首次段才尝试捕获标题
    if seg_idx == 0:
        caption_items = _capture_caption(all_page_items, region, scoped, assigned_ids)
    else:
        caption_items = []

    all_items = _merge_and_sort_items(caption_items, scoped)
    rows = _cluster_items_by_y(all_items)
    rows = _normalize_rows_to_columns(rows)
    col_ranges = _estimate_column_x_ranges(rows)
    col_schema = infer_column_schema(rows)

    y0_val = min(it["y0"] for it in all_items)
    y1_val = max(it["y1"] for it in all_items)

    tables.append({
        "page": page_num,
        "pages": [page_num],
        "y0": y0_val,
        "y1": y1_val,
        "text_items": all_items,
        "rows": rows,
        "row_count": len(rows),
        "is_cross_page": False,
        "caption": _extract_caption(region, caption_items) if seg_idx == 0 else "",
        "region_index": ri,
        "confidence": conf,
        "column_x_ranges": col_ranges,
        "column_schema": col_schema,
    })

    LOG = _get_log()
    LOG.info(
        "  [建表] page=%d region=%d capt='%s' rows=%d cols=%d col_ranges=%s schema=%s",
        page_num, ri,
        tables[-1]["caption"][:40] if tables[-1]["caption"] else "-",
        len(rows), len(col_ranges),
        ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
        ", ".join(cs.get("dominant_type", "?") for cs in col_schema),
    )


def _capture_caption(
    items: List[dict],
    region: dict,
    scoped_items: List[dict],
    assigned_ids: set,
) -> List[dict]:
    """捕获 region 上方的表格标题文本。

    策略：
    1. 先用 region 的 context_text 提取关键词
    2. 在 region y0 上方 margin 范围内搜索包含关键词的 items
    3. 将符合条件的 items 归入表格
    """
    context_text = region.get("context_text", "").strip()
    if not context_text:
        return []

    ry0 = region.get("y0", 0)
    margin = 60  # Y 方向向上扩展量 (pt)

    # 从 context_text 中提取关键短词
    keywords = _extract_keywords(context_text)

    caption_items = []
    for it in items:
        if id(it) in assigned_ids:
            continue
        if it["y1"] < ry0 - margin or it["y0"] > ry0:
            continue  # 不在上方 margin 范围内

        it_text = it["text"].strip()
        for kw in keywords:
            if kw and (kw in it_text or it_text in kw):
                caption_items.append(it)
                assigned_ids.add(id(it))
                break

    return caption_items


def _extract_keywords(text: str) -> List[str]:
    """从 context_text 中提取关键匹配词。"""
    text = re.sub(r'\s+', ' ', text).strip()
    words = text.split()
    # 取较长的词作为特征（避免短词误匹配）
    return [w for w in words if len(w) >= 2]


def _extract_caption(region: dict, caption_items: List[dict]) -> str:
    """提取表格标题文本。"""
    if caption_items:
        caption_items_sorted = sorted(caption_items, key=lambda it: it["x0"])
        return " ".join(it["text"] for it in caption_items_sorted)
    return region.get("context_text", "")


def _merge_and_sort_items(
    caption_items: List[dict],
    scoped_items: List[dict],
) -> List[dict]:
    """合并标题 items 和表格 items，按 Y 排序。"""
    # 标题 items 的 Y 更小，放在前面
    return sorted(
        list(caption_items) + list(scoped_items),
        key=lambda it: (it["y_mid"], it["x0"])
    )


def _build_single_table(
    page_num: int,
    items: List[dict],
    region_index: int,
) -> dict:
    """无 region 时，将全页 items 构建为单张表。"""
    rows = _cluster_items_by_y(items)
    rows = _normalize_rows_to_columns(rows)
    col_ranges = _estimate_column_x_ranges(rows)
    col_schema = infer_column_schema(rows)

    LOG = _get_log()
    LOG.info(
        "  [建单表] page=%d rows=%d cols=%d col_ranges=%s schema=%s",
        page_num, len(rows), len(col_ranges),
        ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
        ", ".join(cs.get("dominant_type", "?") for cs in col_schema),
    )

    return {
        "page": page_num,
        "pages": [page_num],
        "y0": min(it["y0"] for it in items) if items else 0,
        "y1": max(it["y1"] for it in items) if items else 0,
        "text_items": items,
        "rows": rows,
        "row_count": len(rows),
        "is_cross_page": False,
        "caption": "",
        "region_index": region_index,
        "confidence": 0.0,
        "column_x_ranges": col_ranges,
        "column_schema": col_schema,
    }


# ================================================================
# 2.5. 图表误判过滤（P0 优化）
# ================================================================

def _filter_chart_like_tables(
    all_tables: List[tuple],
) -> Tuple[List[tuple], int]:
    """过滤被误判为表格的图表标签区域。

    检测特征：
    1. 坐标轴刻度值（40, 30, 20, 10, 0 等递减/递增序列）
    2. 孤立单字行（图表图例碎片：业、税、金、附、加）
    3. 孤儿百分比（无行标签，仅百分比数值）
    4. 缺少有意义的首列标签（无中文标签行）
    5. 纯图表数据标签（每行只有 1~2 个数值，无标签结构）
    """
    filtered = []
    removed_count = 0

    for page_num, table in all_tables:
        if _is_chart_like(table):
            removed_count += 1
            continue
        filtered.append((page_num, table))

    return filtered, removed_count


def _is_chart_like(table: dict) -> bool:
    """判断一张"表"是否实际上是图表的文本标签。

    综合多维度打分，score >= 2 判定为图表。
    """
    rows = table.get("rows", [])
    if not rows or len(rows) < 2:
        return False

    # 最大列数
    max_cols = max((len(r.get("texts", [])) for r in rows), default=0)

    # 收集所有文本
    all_texts = []
    for row in rows:
        texts = row.get("texts", [])
        all_texts.extend(t for t in texts if t.strip())

    if not all_texts:
        return True  # 空表 → 过滤

    score = 0

    # ---- 维度1: 坐标轴刻度检测 ----
    if _has_axis_tick_pattern(rows):
        score += 3  # 强信号

    # ---- 维度2: 孤立单字行（图例碎片） ----
    if _has_isolated_single_chars(rows):
        score += 3  # 强信号

    # ---- 维度3: 高比例孤立数值（无标签） ----
    if _has_high_isolated_number_ratio(rows):
        score += 2  # 中等信号

    # ---- 维度4: 缺少行标签列 ----
    if _missing_label_column(rows):
        score += 2  # 中等信号

    # ---- 维度5: 饼图切片标签模式 ----
    if _has_pie_chart_pattern(rows):
        score += 2  # 中等信号

    # ---- 维度6: 瀑布图/柱状图数据标签 ----
    if _has_waterfall_label_pattern(rows):
        score += 1  # 弱信号

    # ---- 维度7: 列稀疏度（大部分行只有1~2列有数据 → 图表标签） ----
    if _has_extreme_sparsity(rows, max_cols):
        score += 2  # 中等信号

    # 但如果有明确的表格特征 → 减分（留表，但力度较轻）
    table_chars_score = _has_table_characteristics(rows)
    if table_chars_score >= 3:
        score -= 2  # 强表格特征才大幅减分
    elif table_chars_score >= 1:
        score -= 1  # 弱表格特征轻度减分

    return score >= 2


def _has_axis_tick_pattern(rows: List[dict]) -> bool:
    """检测坐标轴刻度序列：如 40, 30, 20, 10, 0 或 2020, 2021, 2022, 2023, 2024。

    特征：
    - 多行数据每行只有 1~2 个数值（非多列表格结构）
    - 数值呈等差数列递增或递减
    """
    # 收集每行的纯数值（不含标签）
    numeric_sequences = []
    for row in rows:
        texts = row.get("texts", [])
        numbers = []
        for t in texts:
            t = t.strip()
            # 纯数字（可能含千分位逗号、负号、小数点）
            clean = t.replace(",", "").replace(" ", "").replace("(", "").replace(")", "")
            try:
                if clean:
                    float(clean)
                    numbers.append(clean)
            except ValueError:
                pass
        if numbers:
            numeric_sequences.append(numbers)

    if len(numeric_sequences) < 3:
        return False

    # 找同列的数值序列（每行第1个数值）
    col_values = []
    for seq in numeric_sequences:
        if seq:
            col_values.append(float(seq[0]))

    if len(col_values) < 3:
        return False

    # 检查是否为单调递增的等差数列
    diffs = [col_values[i + 1] - col_values[i] for i in range(len(col_values) - 1)]
    if not diffs:
        return False

    # 所有差值同方向 且 差值一致（或递增/递减的"漂亮"数字）
    all_same_sign = all(d > 0 for d in diffs) or all(d < 0 for d in diffs)
    if not all_same_sign:
        return False

    # 判断差值是否"规律性"（如 1, 10, 100, 1000 等整齐步长）
    avg_diff = sum(abs(d) for d in diffs) / len(diffs)
    if avg_diff == 0:
        return True  # 所有值相同 → 图表刻度

    # 如果是"漂亮"的步长 → 很可能是刻度
    nice_steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
                  100000, 1000000]
    for step in nice_steps:
        if abs(avg_diff - step) / step < 0.05:
            return True

    # 差异一致性检查：标准差 / 均值 很小 → 规律步长
    if len(diffs) >= 2:
        abs_diffs = [abs(d) for d in diffs]
        mean = sum(abs_diffs) / len(abs_diffs)
        if mean > 0:
            std = (sum((d - mean) ** 2 for d in abs_diffs) / len(abs_diffs)) ** 0.5
            if std / mean < 0.15:  # 非常一致
                return True

    return False


def _has_isolated_single_chars(rows: List[dict]) -> bool:
    """检测孤立单字行：图表图例被拆成碎片。

    特征：多行中每行只有 1~2 个中文字符，且这些行的列数很少。
    如 ["业", "税", "金", "附", "加"] 分在连续多行。
    """
    single_char_rows = 0
    single_chars = []

    for row in rows:
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue

        # 该行全是短文本（每个 ≤ 2 字符）
        all_short = all(len(t) <= 2 for t in non_empty)
        # 且每个都是纯中文单字/双字
        all_chinese_short = all(
            re.match(r'^[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]{1,2}$', t)
            for t in non_empty
        )

        if all_short and all_chinese_short and len(non_empty) <= 5:
            single_char_rows += 1
            single_chars.extend(non_empty)

    # 至少有 60% 的行是孤立单字行
    return single_char_rows >= max(3, len(rows) * 0.6)


def _has_high_isolated_number_ratio(rows: List[dict]) -> bool:
    """检测高比例孤立数值：大部分行只有数值没有标签。

    图表的数据标签通常只有短期数值（如百分比、坐标值），没有行标签列。
    """
    total_rows = len(rows)
    numeric_only_rows = 0
    label_rows = 0

    for row in rows:
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue

        # 统计数值占比
        num_count = 0
        label_count = 0
        for t in non_empty:
            # 纯数值检测
            clean = t.replace(",", "").replace("%", "").replace(" ", "")
            try:
                float(clean)
                num_count += 1
                continue
            except ValueError:
                pass
            # 含中文 → 标签
            if re.search(r'[\u4e00-\u9fff]', t):
                label_count += 1

        if num_count > 0 and label_count == 0:
            numeric_only_rows += 1
        if label_count > 0:
            label_rows += 1

    # 纯数值行 > 80% 且 标签行 < 1（没有行标签列）
    ratio = numeric_only_rows / total_rows if total_rows > 0 else 0
    return ratio > 0.8 and label_rows <= 1


def _missing_label_column(rows: List[dict]) -> bool:
    """检测是否缺少有意义的首列标签。

    真实表格的首列通常是中文标签（如"营业收入""利息净收入"）。
    图表的"首列"通常是坐标值或空值。
    """
    first_col_texts = []
    for row in rows:
        texts = row.get("texts", [])
        if texts and texts[0].strip():
            first_col_texts.append(texts[0].strip())

    if len(first_col_texts) < 2:
        return True

    # 统计首列中：中文标签 vs 纯数值
    cn_count = sum(1 for t in first_col_texts if re.search(r'[\u4e00-\u9fff]', t))
    num_count = 0
    for t in first_col_texts:
        clean = t.replace(",", "").replace("%", "").replace(" ", "")
        try:
            float(clean)
            num_count += 1
        except ValueError:
            pass

    # 首列几乎全是数值 → 非表格结构
    if num_count >= len(first_col_texts) * 0.7:
        return True

    # 首列几乎全空 → 非表格
    non_empty_ratio = len(first_col_texts) / len(rows)
    return non_empty_ratio < 0.3


def _has_pie_chart_pattern(rows: List[dict]) -> bool:
    """检测饼图标签模式。

    特征：
    - 多个百分比值 + 极少量短文本
    - 每行只有 1~2 个值
    - 这些都是饼图的切片文本被误识别为表格行
    """
    percent_count = 0
    total_cells = 0
    max_cols = 0

    for row in rows:
        texts = row.get("texts", [])
        max_cols = max(max_cols, len(texts))
        total_cells += len(texts)
        for t in texts:
            t = t.strip()
            if t.endswith("%"):
                percent_count += 1

    if total_cells == 0:
        return False

    # 百分比占比 > 40%
    percent_ratio = percent_count / total_cells

    # 列数很少（≤3），且百分比占比高 → 饼图
    if max_cols <= 3 and percent_ratio > 0.4:
        return True

    # 单列表（max_cols==1），大量百分比 → 饼图
    if max_cols <= 1 and percent_count >= 3:
        return True

    return False


def _has_waterfall_label_pattern(rows: List[dict]) -> bool:
    """检测瀑布图/柱状图标签模式。

    特征：数据标签行呈"大值 + 小值"交替（如 1,000,000 和 800,000 交替）
    且缺乏表头行。
    """
    if len(rows) < 3:
        return False

    # 检查首行是否为表头
    first_row_texts = rows[0].get("texts", [])
    has_header = any(re.search(r'[\u4e00-\u9fff]', t) for t in first_row_texts)

    if has_header and len(first_row_texts) >= 3:
        return False  # 有表头+多列 → 可能是真表

    # 检查数值行：每行数值很少（≤2个）
    narrow_rows = 0
    for row in rows:
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if len(non_empty) <= 2:
            narrow_rows += 1

    return narrow_rows >= len(rows) * 0.7


def _has_extreme_sparsity(rows: List[dict], max_cols: int) -> bool:
    """检测极端稀疏列结构：大部分行只有1~2个非空列。

    真实表格：每行各列通常都有数据（表头/数值填满）。
    图表标签：大部分行只有一个值悬浮在某个 X 位置。

    max_cols: 表格的总列数
    """
    if max_cols <= 1 or len(rows) < 3:
        return False

    sparse_row_count = 0
    for row in rows:
        texts = row.get("texts", [])
        non_empty_count = sum(1 for t in texts if t.strip())
        if non_empty_count <= 2 and max_cols >= 3:
            sparse_row_count += 1

    # > 60% 的行在宽表（≥3列）中只有 ≤2 个非空列
    return sparse_row_count >= len(rows) * 0.6


def _has_table_characteristics(rows: List[dict]) -> int:
    """检测明确表格特征，用于纠正误判（反向减分）。

    返回整数值表示确凿程度（越高越确定是真表）。

    特征：
    - 有表头行（中文标签 + 年份词）
    - 有数据行（行标签 + 多列数值）
    - 列结构明确（≥4列）
    - 有较长中文标签
    - 有表格摘要关键词
    """
    if len(rows) < 3:
        return 0

    all_texts = []
    for row in rows:
        all_texts.extend(t.strip() for t in row.get("texts", []) if t.strip())

    score = 0

    # 特征1: 含表格摘要关键词
    table_kw = ["合计", "总计", "总额", "小计", "项目", "指标", "单位", "万元", "千元", "百万元"]
    has_kw = any(kw in t for kw in table_kw for t in all_texts)
    if has_kw:
        score += 1

    # 特征2: 有年份列头
    has_year = any(re.search(r'(19|20)\d{2}年?', t) for t in all_texts)
    if has_year:
        score += 1

    # 特征3: 多列表格（≥4列）且有足够的数据密度
    max_cols = max((len(r.get("texts", [])) for r in rows), default=0)
    if max_cols >= 4:
        # 还需要检查数据密度：每行非空列数
        avg_fill = 0
        for row in rows:
            texts = row.get("texts", [])
            avg_fill += sum(1 for t in texts if t.strip())
        avg_fill = avg_fill / len(rows) if rows else 0
        if avg_fill >= 3:  # 平均每行至少3个非空值 → 真表
            score += 1

    # 特征4: 有较长的中文标签（≥4个中文字）
    has_long_cn = any(
        len(re.findall(r'[\u4e00-\u9fff]', t)) >= 4 for t in all_texts
    )
    if has_long_cn:
        score += 1

    return score


# ================================================================
# 3. 跨页续表拼接（启发式）
# ================================================================

def _merge_cross_page_tables(
    all_tables: List[tuple],
) -> Tuple[List[tuple], int]:
    """检测并拼接跨页续表。

    判断标准：
    1. 两表在连续页码上
    2. 后表首行无表头特征（无中文标签，以数值为主）
    3. 前表末行非合计/总计等终结行
    4. 列数相近（基于 X 聚类列数，容差 ≤ 2）

    Returns:
        (merged_tables, merge_count)
    """
    if len(all_tables) < 2:
        return all_tables, 0

    # 按页码分组，每组内按 Y 排序
    page_groups: Dict[int, List[int]] = {}  # page → [index in all_tables]
    for idx, (pg, _) in enumerate(all_tables):
        page_groups.setdefault(pg, []).append(idx)

    merged_pairs = []  # [(keep_idx, remove_idx), ...]

    for idx in range(len(all_tables) - 1):
        pg_a, table_a = all_tables[idx]
        pg_b, table_b = all_tables[idx + 1]

        if pg_b != pg_a + 1:
            continue  # 非连续页
        if table_a.get("is_cross_page") and table_b.get("is_cross_page"):
            continue  # 已经是跨页表，不再重复合并

        # 检查：table_b 是本页第一个表，table_a 是前页最后一个表
        b_is_first = (page_groups[pg_b][0] == idx + 1)
        a_is_last = (page_groups[pg_a][-1] == idx)
        if not (a_is_last and b_is_first):
            continue

        # 跨页判定
        if _should_merge_continuation(table_a, table_b):
            merged_pairs.append((idx, idx + 1))

    if not merged_pairs:
        return all_tables, 0

    # 执行合并（从后往前处理）
    consumed = set()
    result = []
    merge_count = 0

    # 构建合并图：处理传递性合并 (A→B, B→C → A→C)
    merged_pairs.sort(key=lambda x: x[0])
    i = 0
    while i < len(all_tables):
        if i in consumed:
            i += 1
            continue

        keeper = all_tables[i]
        j = i
        chain = [i]

        # 向前查找合并链
        next_j = j + 1
        while next_j < len(all_tables):
            if (j, next_j) in [(a, b) for a, b in merged_pairs]:
                chain.append(next_j)
                consumed.add(next_j)
                j = next_j
                next_j = j + 1
                merge_count += 1
            else:
                break

        if len(chain) > 1:
            # 合并链上所有表
            _, keeper_dict = keeper
            for merged_idx in chain[1:]:
                _, merged_dict = all_tables[merged_idx]
                keeper_dict = _concat_continuation_table(keeper_dict, merged_dict)
            keeper = (keeper[0], keeper_dict)

        result.append(keeper)
        i = j + 1 if len(chain) > 1 else i + 1

    return result, merge_count


def _should_merge_continuation(table_a: dict, table_b: dict) -> bool:
    """判断 table_b 是否为 table_a 的跨页续表。

    多层判定（优先级从高到低）：
    1. 列数对比：X 聚类列数差异 ≤ 2
    2. 后表无表头：首行以数值为主 + 列签名判定
    3. 前表末行非终结：不含 合计/总计 等关键词
    4. 后表从页面顶部开始：y0 在页面 15% 以内
    """
    # ---- 列数对比 ----
    cols_a = len(table_a.get("column_x_ranges", []))
    cols_b = len(table_b.get("column_x_ranges", []))

    if cols_a == 0 or cols_b == 0:
        # X 范围估计失败 → 用 row items 数量估计
        rows_a = table_a.get("rows", [])
        rows_b = table_b.get("rows", [])
        if rows_a:
            cols_a = max(len(r.get("texts", [])) for r in rows_a)
        if rows_b:
            cols_b = max(len(r.get("texts", [])) for r in rows_b)

    if cols_a == 0 or cols_b == 0:
        return False
    if abs(cols_a - cols_b) > 2:
        return False

    # ---- 后表完整表头检测（多行扫描，优于单行判定） ----
    if _has_complete_table_header(table_b):
        return False

    # ---- 后表无表头 ----
    rows_b = table_b.get("rows", [])
    if not rows_b:
        return False
    first_row_b = rows_b[0]
    first_texts = first_row_b.get("texts", [])

    # 用列签名分值制判断后表首行是否为表头
    col_schema_b = table_b.get("column_schema", [])
    if col_schema_b:
        header_score = _score_table_row(first_texts, col_schema_b)
        # 得分 < 2 或没有数值特征 → 更可能是表头
        has_numeric = any(
            t.replace(",", "").replace(".", "").replace("-", "").replace("(", "").replace(")", "").isdigit()
            for t in first_texts
        )
        if header_score < 2:
            # 得分很低 → 几乎确定是表头
            return False
        if not has_numeric:
            # 纯文本行 → 表头
            return False

    # 原有中文占比检测（作为补充）
    total_len = sum(len(t) for t in first_texts)
    if total_len == 0:
        return _check_prev_table_ends_with_data(table_a)

    chinese_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t)) for t in first_texts)
    chinese_ratio = chinese_chars / total_len if total_len > 0 else 0

    if chinese_ratio >= 0.4 and len(first_texts) >= 2:
        if _is_header_like(first_texts):
            return False

    # ---- 前表末行非终结（P2 优化：多子表结构放宽） ----
    has_subtables = _has_multiple_sub_table_structure(table_a)
    if not _check_prev_table_ends_with_data(table_a):
        if has_subtables:
            # 多子表结构的"合计"很可能是子表内部的汇总，
            # 而非整个表格的终结，允许跨页拼接
            pass
        else:
            return False

    # ---- 后表位置检查 ----
    y0_b = table_b.get("y0", 0)
    if y0_b > 200:
        pass

    return True


def _has_multiple_sub_table_structure(table: dict) -> bool:
    """检测表格是否包含多个子表结构（P2 优化）。

    多子表特征：
    1. 有空白分隔行（全空行），将数据分成多个区块
    2. 有重复的子表头模式（如每个子表都有"金额""占比"列头）
    3. 表格非常宽（≥6列），可能有子表并列

    返回 True 表示该表包含多子表，跨页拼接时对末行汇总更宽容。
    """
    rows = table.get("rows", [])
    if len(rows) < 5:
        return False

    # 特征1: 空白分隔行检测
    blank_separator_count = 0
    blank_positions = []
    for ri, row in enumerate(rows):
        texts = row.get("texts", [])
        non_empty = [t for t in texts if t.strip()]
        if len(non_empty) <= 1:  # 整行为空或只有1个值
            blank_separator_count += 1
            blank_positions.append(ri)

    # 至少2个空白分隔行 且 不是都在头尾 → 内部有子表边界
    if blank_separator_count >= 2:
        mid_positions = [p for p in blank_positions if 1 < p < len(rows) - 2]
        if mid_positions:
            return True

    # 特征2: 检测汇总行模式（多个"合计"分散在不同位置）
    summary_kw = SUMMARY_KW
    summary_rows = []
    for ri, row in enumerate(rows):
        all_text = "".join(row.get("texts", []))
        if any(kw in all_text for kw in summary_kw):
            summary_rows.append(ri)

    # 有 ≥2 个汇总行且不在相邻行 → 多个子表
    if len(summary_rows) >= 2:
        for i in range(len(summary_rows) - 1):
            if summary_rows[i + 1] - summary_rows[i] > 2:
                return True

    # 特征3: 检测子表头模式（行标签+列头组合 重复出现）
    # 在数据行中间出现类似表头的行为 → 子表边界
    header_like_rows = []
    for ri, row in enumerate(rows[1:], 1):  # 跳过首行（可能是主表头）
        texts = row.get("texts", [])
        if _is_header_like(texts):
            header_like_rows.append(ri)

    if len(header_like_rows) >= 2:
        for i in range(len(header_like_rows) - 1):
            if header_like_rows[i + 1] - header_like_rows[i] > 3:
                return True

    # 特征4: 列数很多（≥6列），典型的宽表多子结构
    max_cols = max((len(r.get("texts", [])) for r in rows), default=0)
    if max_cols >= 6 and len(summary_rows) >= 1:
        return True

    return False


def _is_header_like(texts: List[str]) -> bool:
    """判断文本列表是否像表头行（列标题）。

    表头特征：多个短中文标签 + 年份词 + 变化/增减词。
    如 ["项目", "2024年", "2023年", "变化+/(-)"] 是表头。
    如 ["营业收入", "36,874,848", "38,251,377", "3.7%"] 是数据行。
    """
    # 检查是否含年份（使用统一正则）
    has_year = any(YEAR_PATTERN.search(t) for t in texts)

    # 检查是否含 变化/增减 关键词（使用统一常量）
    has_delta = any(any(kw in t for kw in DELTA_KW) for t in texts)

    if has_year and has_delta:
        return True

    # 全是短文本且无长数字
    has_long_numeric = any(
        re.search(r'\d[\d,.]{3,}', t) for t in texts
    )
    all_short = all(len(t) <= 6 for t in texts)

    if all_short and not has_long_numeric and has_year:
        return True

    return False


def _has_complete_table_header(table: dict) -> bool:
    """扫描后表前 N 行，判断是否有独立完整的表头（含前置纯文本描述）。

    判定为独立完整表格的条件（满足任一）：
    1. 表上方有纯文本描述（含章节标记如"（续）""附注"，或无数值的长中文行）
    2. 同时存在年份表头（≥2 个年份单元格）和实体表头（本集团/本行等）

    这些信号说明后表是独立的新表格，不应作为续表合并到前表。

    扫描时遇到数据行立即停止，避免把数据区内容误判为表头。
    """
    rows = table.get("rows", [])
    if not rows:
        return False

    scan_limit = min(len(rows), 8)

    has_section_title = False
    has_year_hdr = False
    has_entity_hdr = False

    SECTION_KW = ['（续）', '(续)', '附注', '注释']

    for ri in range(scan_limit):
        texts = rows[ri].get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue

        all_text = "".join(non_empty)

        # ---- 信号1：纯文本描述 / 章节标记 ----
        # 含章节关键词
        if any(kw in all_text for kw in SECTION_KW):
            has_section_title = True

        # 无数值 + 长中文 → 段落描述（如 "(a) 以摊余成本计量的..."）
        if not has_section_title:
            has_numeric = any(_is_numeric_cell(t) for t in non_empty)
            cn_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t)) for t in non_empty)
            if not has_numeric and cn_chars >= 6:
                has_section_title = True

        # ---- 信号2：年份表头（≥2 个年份单元格） ----
        year_count = sum(1 for t in non_empty if YEAR_PATTERN.search(t))
        if year_count >= 2:
            has_year_hdr = True

        # ---- 信号3：实体表头（精确单元格匹配） ----
        if any(t in ENTITY_KW for t in non_empty):
            has_entity_hdr = True

        # ---- 停止条件：遇到数据行（多数单元格是数值） ----
        num_count = sum(1 for t in non_empty if _is_numeric_cell(t))
        if num_count >= max(1, len(non_empty) // 2) and num_count >= 2:
            break

    # 判定
    if has_section_title:
        return True
    if has_year_hdr and has_entity_hdr:
        return True
    return False


def _check_prev_table_ends_with_data(table: dict) -> bool:
    """检查前表末行是否为数据行（非合计/总计等终结行）。"""
    rows = table.get("rows", [])
    if len(rows) < 1:
        return True  # 无数据，允许合并（反正空表）

    last_row = rows[-1]
    last_texts = last_row.get("texts", [])
    all_text = "".join(last_texts)

    if any(kw in all_text for kw in SUMMARY_KW):
        return False

    return True


def _concat_continuation_table(table_a: dict, table_b: dict) -> dict:
    """将 table_b 作为续表拼接到 table_a。"""
    # Y 偏移量（用于调整 table_b items 的 Y 坐标）
    y_offset = table_a["y1"] - table_b["y0"] + 10.0

    # 调整 table_b 的 items
    adjusted_items = []
    for it in table_b["text_items"]:
        it_adj = dict(it)
        it_adj["y0"] = it["y0"] + y_offset
        it_adj["y1"] = it["y1"] + y_offset
        it_adj["y_mid"] = it["y_mid"] + y_offset
        adjusted_items.append(it_adj)

    # 合并 items + 重新聚类
    all_items = table_a["text_items"] + adjusted_items
    all_rows = _cluster_items_by_y(all_items)

    # 去重：检查 table_a 末尾和 table_b 开头是否有重叠行
    all_rows = _dedup_overlap_rows(all_rows, len(table_a["rows"]))

    all_rows = _normalize_rows_to_columns(all_rows)
    col_ranges = _estimate_column_x_ranges(all_rows)
    col_schema = infer_column_schema(all_rows)

    return {
        "page": table_a["page"],
        "pages": sorted(set(table_a.get("pages", [table_a["page"]]) + table_b.get("pages", [table_b["page"]]))),
        "y0": table_a["y0"],
        "y1": table_b["y1"] + y_offset,
        "text_items": all_items,
        "rows": all_rows,
        "row_count": len(all_rows),
        "is_cross_page": True,
        "caption": table_a.get("caption", "") or table_b.get("caption", ""),
        "region_index": table_a.get("region_index", -1),
        "confidence": table_a.get("confidence", 0),
        "column_x_ranges": col_ranges,
        "column_schema": col_schema,
    }


def _dedup_overlap_rows(all_rows: List[dict], split_idx: int) -> List[dict]:
    """检测并移除跨页合并后的重叠行（续表可能重复最后几行数据）。

    Args:
        all_rows: 合并后全部行
        split_idx: 原 table_a 的行数（分界点）

    Returns:
        去重后的行列表
    """
    if split_idx <= 1 or split_idx >= len(all_rows):
        return all_rows

    # 在 split_idx 附近查找重叠（前后各看 3 行）
    check_range_a = range(max(0, split_idx - 3), split_idx)
    check_range_b = range(split_idx, min(len(all_rows), split_idx + 3))

    remove_indices = set()
    for ai in check_range_a:
        row_a_texts = set(
            _normalize_for_search(t) for t in all_rows[ai].get("texts", []) if t.strip()
        )
        if not row_a_texts:
            continue
        for bi in check_range_b:
            row_b_texts = set(
                _normalize_for_search(t) for t in all_rows[bi].get("texts", []) if t.strip()
            )
            if not row_b_texts:
                continue
            # Jaccard 相似度
            intersection = row_a_texts & row_b_texts
            union = row_a_texts | row_b_texts
            if union and len(intersection) / len(union) >= 0.6:
                # 保留更完整的行（item 数多的）
                if len(all_rows[ai].get("texts", [])) >= len(all_rows[bi].get("texts", [])):
                    remove_indices.add(bi)
                else:
                    remove_indices.add(ai)

    if remove_indices:
        return [r for i, r in enumerate(all_rows) if i not in remove_indices]
    return all_rows


def _get_segment_caption(
    parent_caption: str,
    seg_rows: list,
    seg_idx: int,
    total_segments: int,
) -> str:
    """为混合表拆分后的子表生成合适的 caption。
    
    策略：
    - seg_idx==0：保留父表原始标题
    - seg_idx>0：从段首行文本中提取描述性文字作为标题
    
    检查段内前3行，寻找包含较长中文描述的文本行作为该子表的独立标题。
    """
    if seg_idx == 0 or total_segments <= 1:
        return parent_caption or ""
    
    # 从 segment 前几行提取潜在的标题文本
    for row in seg_rows[:3]:
        texts = row.get("texts", [])
        if not texts:
            continue
        # 拼接本行所有文本
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        if not joined:
            continue
        # 判断是否适合作为标题：至少有10个字符，且包含中文
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in joined)
        has_digit = any(c.isdigit() for c in joined)
        # 标题特征：有中文、长度足够、不是纯数字
        if has_cjk and len(joined) >= 10:
            return joined
        # 如果只有数字和少量英文（如表头行"2024年 2023年"），跳过继续找更有描述性的行
        if has_digit and len(joined) < 15 and not has_cjk:
            continue
        if has_cjk:
            return joined
    
    # 回退：用"(续)"标记
    return f"{parent_caption or ''}(续)"


# ================================================================
# 4. 行 normalization：对齐到统一列结构
# ================================================================

def _normalize_rows_to_columns(
    rows: List[dict],
    col_ranges: Optional[List[Tuple[float, float]]] = None,
) -> List[dict]:
    """将每行的 items 按 X 坐标对齐到统一列结构（零数据丢失版）。

    核心原则：绝不丢弃任何 item。遇到碰撞时拼接保留，如果归一化后
    非空值减少则退回到原始数据。

    Args:
        rows: _cluster_items_by_y 的输出
        col_ranges: 列 X 范围（可选，不传则从最宽行自动推断）

    Returns:
        规范化后的行列表，每行 texts 长度统一为列数，空列=""
    """
    from codes.table_validator.cell_differ import _normalize_for_search

    if col_ranges is None:
        col_ranges = _detect_column_ranges_from_rows(rows)

    n_cols = len(col_ranges)
    if n_cols <= 1:
        return rows

    # 计算每列的 X 中心点（用于最近中心映射）
    col_centers = [(x0 + x1) / 2 for x0, x1 in col_ranges]

    normalized = []
    for row in rows:
        items = row.get("items", [])
        original_texts = row.get("texts", [])
        original_non_empty = sum(1 for t in original_texts if t.strip())

        # 不需要对齐的行：items 数 ≥ 列数 → 直接保留
        if len(items) >= n_cols:
            normalized.append(row)
            continue

        # 按 X 中心点找最近列
        col_texts: Dict[int, str] = {}
        for it in items:
            x0 = it.get("x0", 0)
            x1 = it.get("x1", 0)
            text = it.get("text", "")
            xc = (x0 + x1) / 2

            # 找 X 中心最近的列
            best_col = 0
            best_dist = float("inf")
            for ci, cc in enumerate(col_centers):
                dist = abs(xc - cc)
                if dist < best_dist:
                    best_dist = dist
                    best_col = ci

            # 碰撞处理：同一列已有值 → 拼接到已有文本后面
            existing = col_texts.get(best_col, "")
            if existing and text:
                col_texts[best_col] = existing + " " + text
            else:
                col_texts[best_col] = text

        aligned_texts = [col_texts.get(ci, "") for ci in range(n_cols)]

        # 安全检查：归一化后非空项数不得少于原始
        aligned_non_empty = sum(1 for t in aligned_texts if t.strip())
        if aligned_non_empty < original_non_empty:
            # 数据丢失 → 退回：保留原始 texts 并补齐列数
            aligned_texts = list(original_texts)
            while len(aligned_texts) < n_cols:
                aligned_texts.append("")

        normalized.append({
            "items": items,
            "y_min": row.get("y_min", 0),
            "y_max": row.get("y_max", 0),
            "texts": aligned_texts,
            "norm_texts": [_normalize_for_search(t) for t in aligned_texts],
            "col_x_ranges": [
                (it.get("x0", 0), it.get("x1", 0)) for it in items
            ],
        })

    return normalized


def _detect_column_ranges_from_rows(rows: List[dict]) -> List[Tuple[float, float]]:
    """从最宽行的 items X 坐标推断列结构。

    用 item 最多的行作为模板，取其 items 的 (x0, x1) 即列范围。
    这比从所有行聚类更精确，因为最宽行天然代表完整的列集合。
    """
    if not rows:
        return []

    # 找 item 最多的行
    best_row = max(rows, key=lambda r: len(r.get("items", [])))
    items = best_row.get("items", [])

    if not items:
        return []

    col_ranges = []
    for it in items:
        x0 = it.get("x0", 0)
        x1 = it.get("x1", 0)
        if x1 > x0:
            col_ranges.append((x0, x1))

    # 如果最宽行 item 太少（<2），回退到所有行统计
    if len(col_ranges) < 2:
        return _estimate_column_x_ranges(rows)

    return col_ranges


# ================================================================
# 5. 列 X 范围估计（原版，用于兼容）
# ================================================================

def _merge_sparse_columns_by_rows(
    rows: List[dict],
    col_ranges: List[Tuple[float, float]],
    min_row_ratio: float = 0.15,
    min_abs_rows: int = 3,
) -> List[Tuple[float, float]]:
    """合并稀疏列（实际有文本的行数过少）到最近的非稀疏列。

    解决 x0 聚类可能把缩进标签、表头居中偏移等产生的小簇误判为
    独立列的问题：例如缩进子项"优先股"的 x0 与父项"其他权益工具"
    略有偏移，可能被聚成单独的列，导致输出出现大量空白列。
    """
    if len(col_ranges) <= 1:
        return col_ranges

    n_rows = len(rows)
    n_cols = len(col_ranges)
    threshold = max(int(n_rows * min_row_ratio), min_abs_rows)

    # 统计每列覆盖了多少行（按行去重）
    col_row_sets: List[set] = [set() for _ in range(n_cols)]
    for row_idx, row in enumerate(rows):
        for it in row.get("items", []):
            text = it.get("text", "").strip()
            if not text:
                continue
            x0 = it.get("x0", 0)
            for ci, (cx0, cx1) in enumerate(col_ranges):
                if cx0 <= x0 <= cx1:
                    col_row_sets[ci].add(row_idx)
                    break

    # 判断哪些列稀疏
    sparse_mask = [len(col_row_sets[ci]) < threshold for ci in range(n_cols)]
    if not any(sparse_mask):
        return col_ranges

    # ---- 日志：记录稀疏列详情 ----
    LOG = _get_log()
    for ci in range(n_cols):
        if sparse_mask[ci]:
            ratio = len(col_row_sets[ci]) / n_rows * 100 if n_rows else 0
            LOG.debug(
                "  [稀疏列合并] 检测到稀疏列 #%d: x=[%.1f, %.1f], "
                "覆盖行=%d/%d (%.0f%%), 阈值=%d — 将合并",
                ci, col_ranges[ci][0], col_ranges[ci][1],
                len(col_row_sets[ci]), n_rows, ratio, threshold,
            )

    # 将稀疏列合并到最近的非稀疏列：扩展目标列范围，然后移除稀疏列
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

    result = [expanded[i] for i in range(len(expanded)) if i not in sparse_set]
    # 兜底：全部列都被合并时，至少保留 2 列（首列和末列）
    # 避免多列表格被完全压成单列，导致混合表拆分逻辑失效
    if len(result) <= 1 and n_cols >= 3:
        result = [col_ranges[0], col_ranges[-1]]
        LOG.debug(
            "  [稀疏列合并] 全部列稀疏，兜底保留首尾2列: %s",
            ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in result),
        )
    LOG.debug(
        "  [稀疏列合并] 完成: %d 列 → %d 列, 合并了 %d 个稀疏列",
        len(col_ranges), len(result), len(sparse_set),
    )
    return result


def _estimate_column_x_ranges(rows: List[dict]) -> List[Tuple[float, float]]:
    """从所有行的 X 坐标统计出列的 X 范围。

    策略：
    1. 统计所有 item 的 X 坐标，对 x0 做聚类
    2. 每个聚类 = 一个列，x0_min ~ x1_max = 该列 X 范围
    3. 聚类阈值自适应：按 x0 间距分布自动选取
    4. 合并稀疏列：x0 聚类可能产生幽灵列（如缩进标签偏移），合并到最近非稀疏列
    """
    if not rows:
        return []

    # 收集所有 item 的 (x0, x1)
    all_x_pairs = []
    for row in rows:
        for it in row.get("items", []):
            x0 = it.get("x0", 0)
            x1 = it.get("x1", 0)
            if x1 > x0:
                all_x_pairs.append((x0, x1))

    if not all_x_pairs:
        return []

    # 按 x0 排序
    all_x_pairs.sort(key=lambda p: p[0])

    # 对 x0 做自适应聚类（间隙 > 平均间距的 3 倍 → 新列）
    x0s = [p[0] for p in all_x_pairs]
    gaps = [x0s[i + 1] - x0s[i] for i in range(len(x0s) - 1) if x0s[i + 1] > x0s[i]]
    if not gaps:
        return [(all_x_pairs[0][0], all_x_pairs[-1][1])]

    avg_gap = sum(gaps) / len(gaps)
    threshold = max(avg_gap * 2.5, 8.0)  # 至少 8pt

    # 聚类
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

    # 每个聚类 → (x0_min, x1_max)
    col_ranges = []
    for cluster in clusters:
        x0_min = min(p[0] for p in cluster)
        x1_max = max(p[1] for p in cluster)
        col_ranges.append((x0_min, x1_max))

    LOG = _get_log()
    LOG.debug(
        "  [列估计] x0聚类得出 %d 列: %s",
        len(col_ranges),
        ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
    )

    # ---- 列范围去重叠 ----
    # x0 聚类得到的列范围使用 (x0_min, x1_max)，但 x1_max 可能
    # 跨越多个列（如该列包含跨列合并单元格），导致宽列的 [x0, x1] 区间
    # 覆盖相邻窄列，在稀疏列合并的归属判断中"偷走"窄列的 item。
    # 解决：用相邻列 x0 的中点作为列右边界，确保各列 x0 区间不重叠。
    if len(col_ranges) > 1:
        refined = []
        for i in range(len(col_ranges)):
            x0 = col_ranges[i][0]
            if i < len(col_ranges) - 1:
                mid = (x0 + col_ranges[i + 1][0]) / 2
                refined.append((x0, mid))
            else:
                refined.append((x0, col_ranges[i][1] + 10))
        col_ranges = refined
        LOG.debug(
            "  [列估计] 去重叠后 %d 列: %s",
            len(col_ranges),
            ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
        )

    # 合并稀疏列（幽灵列）：缩进标签等可能被误判为独立列
    col_ranges = _merge_sparse_columns_by_rows(rows, col_ranges)

    if col_ranges:
        LOG.debug(
            "  [列估计] 最终列结构 %d 列: %s",
            len(col_ranges),
            ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
        )

    return col_ranges


# ================================================================
# 5. 验证报告
# ================================================================

def _generate_report(
    tables: List[dict],
    liteparse_data: dict,
    cross_page_merges: int,
) -> dict:
    """生成分割验证报告。"""
    pages = liteparse_data.get("pages", [])
    all_page_nums = {p.get("page_number", 0): p for p in pages}

    # 按页统计归属
    page_details = {}
    orphan_page_items = {}

    for table in tables:
        for pg in table.get("pages", [table["page"]]):
            if pg not in all_page_nums:
                continue
            if pg not in page_details:
                page_details[pg] = {
                    "tables": 0,
                    "items_total": 0,
                    "items_assigned": 0,
                    "orphans": 0,
                }
            page_details[pg]["tables"] += 1

    # 逐页统计覆盖度
    for pg, lp_page in all_page_nums.items():
        items_raw = lp_page.get("text_items", [])
        if not items_raw:
            continue
        if pg not in page_details:
            continue

        total = len(items_raw)
        page_details[pg]["items_total"] = total

        # 构建该页原始 item 的 key 集合（用于交集裁剪，防止 orphan 负数）
        original_keys = set()
        for it_raw in items_raw:
            key = (
                it_raw.get("text", "").strip(),
                round(it_raw.get("x0", 0), 1),
                round(it_raw.get("y0", 0), 1),
            )
            if it_raw.get("text", "").strip():
                original_keys.add(key)

        # 统计该页已归属的 items
        assigned = set()
        for table in tables:
            if pg in table.get("pages", [table["page"]]):
                for it in table.get("text_items", []):
                    # 用 (text, x0, y0) 做唯一标识（因为跨页合并后 id() 变了）
                    key = (it.get("text", ""), round(it.get("x0", 0), 1), round(it.get("y0", 0), 1))
                    assigned.add(key)

        # 交集裁剪：只统计该页原始存在的 item，防止跨页合并导致 assigned 膨胀
        assigned = assigned & original_keys
        page_details[pg]["items_assigned"] = len(assigned)
        orphan_count = total - len(assigned)
        page_details[pg]["orphans"] = max(orphan_count, 0)  # 确保非负

        # 孤儿 items 详情
        if page_details[pg]["orphans"] > 0:
            orphan_items = []
            for it_raw in items_raw:
                key = (
                    it_raw.get("text", "").strip(),
                    round(it_raw.get("x0", 0), 1),
                    round(it_raw.get("y0", 0), 1),
                )
                if key not in assigned and it_raw.get("text", "").strip():
                    orphan_items.append({
                        "text": it_raw.get("text", ""),
                        "x0": it_raw.get("x0", 0),
                        "y0": it_raw.get("y0", 0),
                        "y1": it_raw.get("y1", 0),
                    })
            if orphan_items:
                orphan_page_items[pg] = orphan_items

    # 表格摘要
    table_summaries = []
    for idx, t in enumerate(tables):
        rows = t.get("rows", [])
        first_3 = []
        for r in rows[:3]:
            first_3.extend(r.get("texts", [])[:3])
        last_3 = []
        for r in rows[-3:]:
            last_3.extend(r.get("texts", [])[-3:])

        table_summaries.append({
            "table_id": t.get("table_id", idx),
            "page": t["page"],
            "pages": t["pages"],
            "items_count": len(t.get("text_items", [])),
            "row_count": t["row_count"],
            "is_cross_page": t.get("is_cross_page", False),
            "caption": t.get("caption", ""),
            "confidence": t.get("confidence", 0),
            "first_items": first_3[:6],
            "last_items": last_3[-6:],
        })

    return {
        "total_pages": len(all_page_nums),
        "table_pages": len(page_details),
        "total_tables": len(tables),
        "cross_page_merges": cross_page_merges,
        "page_details": page_details,
        "orphan_page_items": orphan_page_items,
        "table_summaries": table_summaries,
    }


# ================================================================
# 6. 工具函数
# ================================================================

def _build_items(text_items: List[dict]) -> List[dict]:
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


def _is_likely_table_content(items: List[dict]) -> bool:
    """判断一批 items 是否像表格内容（有数值 + 标签）。"""
    if len(items) < 3:
        return False
    numeric_count = sum(
        1 for it in items
        if re.search(r'[\d,.]{2,}', it["text"])
    )
    return numeric_count >= 2


# ================================================================
# 6.1. 文本启发式 fallback：检测 liteparse 漏掉的内嵌小表格
# ================================================================

def _detect_tables_from_text_clusters(
    page_num: int,
    items: List[dict],
    min_data_rows: int = 3,
    min_cols: int = 2,
) -> List[dict]:
    """文本启发式 fallback：从 text_items 中检测 liteparse 漏掉的内嵌表格。

    适用场景：年报附注页等文本密集型页面中，嵌入的 3~10 行数值型小表格。
    liteparse 的 ML 模型对这些小表召回率较低，但规则可以准确识别。

    策略：
    1. Y 聚类 → 逻辑行
    2. 规范化到统一列结构
    3. 按行扫描，识别「连续含数值的数据行」区段
    4. 汇总行（合计/总计/小计）视为数据行的自然结束
    5. 区段 ≥ min_data_rows 行 → 构建候选表

    Args:
        page_num: 页码
        items: _build_items() 标准化后的 text_items
        min_data_rows: 最少数据行数（含数值的行），默认 3
        min_cols: 最少列数，默认 2

    Returns:
        表格字典列表，格式与 _build_single_table 一致
    """
    if len(items) < 6:
        return []

    LOG = _get_log()

    # 1. Y 聚类成行
    rows = _cluster_items_by_y(items)
    if len(rows) < min_data_rows:
        return []

    # 2. 规范化到列结构
    try:
        rows = _normalize_rows_to_columns(rows)
    except Exception:
        return []

    # 确保每行都有 "texts" 字段
    for row in rows:
        if "texts" not in row:
            row["texts"] = [it.get("text", "") for it in row.get("items", [])]
        if "items" not in row:
            row["items"] = []

    # 3. 扫描连续的数据行区段
    tables = []
    i = 0
    n = len(rows)

    while i <= n - min_data_rows:
        row_texts = rows[i].get("texts", [])
        non_empty = [t.strip() for t in row_texts if t.strip()]

        # 数据行判断：非首列至少有一个数值型单元格
        if not _is_likely_data_row(row_texts, non_empty):
            i += 1
            continue

        # 同时检查该行是否至少有 min_cols 个有内容的列
        if len(non_empty) < min_cols:
            i += 1
            continue

        # 发现数据行 → 前看一行是否为双实体表头（"本集团"+"本行"等），
        # 若是则回退纳入，确保表头行紧挨表格不丢失
        if i > 0:
            prev_texts = rows[i - 1].get("texts", [])
            prev_ne = [t.strip() for t in prev_texts if t.strip()]
            if _is_dual_entity_header_row(prev_ne):
                i -= 1

        # 向后扫描连续数据行
        j = i + 1
        while j < n:
            nxt_texts = rows[j].get("texts", [])
            nxt_non_empty = [t.strip() for t in nxt_texts if t.strip()]
            all_text = "".join(nxt_non_empty)

            # 汇总行：包含在内，并结束该区段
            if any(kw in all_text for kw in ("合计", "总计", "小计", "累计")):
                j += 1
                break

            # 数据行：继续
            if _is_likely_data_row(nxt_texts, nxt_non_empty):
                j += 1
                continue

            # 双实体表头（"本集团"+"本行"等同时出现）→ 确定性表头，紧挨下一行是数据行则穿透
            if _is_dual_entity_header_row(nxt_non_empty):
                if j + 1 < n:
                    lookahead = rows[j + 1].get("texts", [])
                    lookahead_ne = [t.strip() for t in lookahead if t.strip()]
                    if _is_likely_data_row(lookahead, lookahead_ne):
                        j += 1
                        continue

            # 表头行或标题行（有中文无数值）：如果后面还有更多数据行，跳过
            # 如 "2024年    2023年" "12月31日  12月31日"
            has_chinese = any(
                re.search(r'[\u4e00-\u9fff]', t) for t in nxt_non_empty
            )
            has_numeric = any(
                _is_cell_numeric_like(t) for t in nxt_non_empty
            )
            if has_chinese and not has_numeric:
                # 可能是表头行 → 检查下下行是否为数据行
                if j + 1 < n:
                    lookahead = rows[j + 1].get("texts", [])
                    lookahead_ne = [t.strip() for t in lookahead if t.strip()]
                    if _is_likely_data_row(lookahead, lookahead_ne):
                        j += 1
                        continue

            # 空行或纯分隔符 → 结束区段
            break

        segment_rows = rows[i:j]

        # 4. 验证：统计含数值的数据行数
        data_row_count = 0
        for row in segment_rows:
            texts = [t.strip() for t in row.get("texts", []) if t.strip()]
            if _is_likely_data_row(texts, texts):
                data_row_count += 1

        if data_row_count >= min_data_rows:
            # 收集该区段所有 item（扁平化）
            seg_items = []
            for row in segment_rows:
                seg_items.extend(row.get("items", []))

            if not seg_items:
                i = j
                continue

            col_ranges = _estimate_column_x_ranges(segment_rows)
            col_schema = infer_column_schema(segment_rows)

            tables.append({
                "page": page_num,
                "pages": [page_num],
                "y0": min(it["y0"] for it in seg_items),
                "y1": max(it["y1"] for it in seg_items),
                "text_items": seg_items,
                "rows": segment_rows,
                "row_count": len(segment_rows),
                "is_cross_page": False,
                "caption": "",
                "region_index": -1,
                "confidence": 0.0,
                "column_x_ranges": col_ranges,
                "column_schema": col_schema,
                "segment_source": "text_heuristic_fallback",
            })

            LOG.info(
                "  [文本启发式] page=%d 检测到内嵌表格: rows=%d data_rows=%d cols=%d",
                page_num, len(segment_rows), data_row_count, len(col_ranges),
            )

        i = j

    return tables


# ================================================================
# 7. Phase 4: 质量优化后处理（6 大功能）
# ================================================================


# ---- (1) 非财务表格过滤 ----

def _filter_non_financial_tables(tables: List[dict]) -> List[dict]:
    """标记纯文本类非财务表格。

    财务表格特征：至少有一列或多列数值数据。
    纯文本表：所有列都是文本，没有任何数值列 → is_real_table = False。

    不删除表格，仅标记，方便人工核查。
    """
    for table in tables:
        rows = table.get("rows", [])
        if not rows or len(rows) < 2:
            table["is_real_table"] = False
            table["table_category"] = "空表"
            table["quality_reason"] = "行数不足"
            continue

        # 收集所有文本
        all_texts = []
        for row in rows:
            all_texts.extend(t.strip() for t in row.get("texts", []) if t.strip())

        # 检测是否有数值
        numeric_count = 0
        for t in all_texts:
            clean = t.replace(",", "").replace("%", "").replace(" ", "")
            if clean.startswith("(") and clean.endswith(")"):
                clean = "-" + clean[1:-1]
            try:
                float(clean)
                numeric_count += 1
            except (ValueError, TypeError):
                pass

        total_cells = len(all_texts)
        numeric_ratio = numeric_count / total_cells if total_cells > 0 else 0

        # 检查各列数值占比（排除前2行表头区域）
        max_cols = max((len(r.get("texts", [])) for r in rows), default=0)
        has_numeric_col = False
        if max_cols >= 2:
            for c in range(max_cols):
                col_vals = []
                for ri, row in enumerate(rows):
                    texts = row.get("texts", [])
                    if c < len(texts) and ri >= 2:  # 跳过前2行（可能是表头）
                        col_vals.append(texts[c].strip())
                if not col_vals:
                    continue
                col_numeric = 0
                for v in col_vals:
                    if not v:
                        continue
                    clean = v.replace(",", "").replace("%", "").replace(" ", "")
                    try:
                        float(clean)
                        col_numeric += 1
                    except (ValueError, TypeError):
                        pass
                if len([v for v in col_vals if v]) > 0:
                    col_ratio = col_numeric / max(len([v for v in col_vals if v]), 1)
                    if col_ratio >= 0.5:
                        has_numeric_col = True
                        break

        if has_numeric_col or numeric_ratio >= 0.15:
            table["is_real_table"] = True
            table["has_numeric_data"] = True
        else:
            table["is_real_table"] = False
            table["has_numeric_data"] = False
            table["quality_reason"] = "纯文本数据，无数值列"

    return tables


# ---- (2) 多表混合检测与拆分 ----

# 调试开关：设为 True 可在控制台输出多表拆分的详细诊断日志
_DEBUG_MIXED_SPLIT = False


def _detect_and_split_mixed_tables(tables: List[dict]) -> Tuple[List[dict], int]:
    """检测一张表中是否混入了多个表格（中间出现表头行），主动切分。

    多层检测逻辑：
    Tier 1（高置信度）：
        - 常规：年份词/变化词 + 文本为主 + ≥2 非空单元格 + 前有数据 → 拆分
        - 单单元格年份分隔行：含年份 + 无变化词 + 仅1个文本单元格 + 前有数据 → 拆分
          （如 PDF 居中渲染的 "2023年12月31日" 年度分隔标签）
    Tier 1.5（中高置信度）：纯文本、含长中文（≥10字）、前面紧邻 ≥2 行数据 → 拆分
        （用于检测表格间的中文标题描述行，如"下表列出于所示期间本集团按区域...")
    Tier 1.6（中高置信度）：合计/总计行（含关键词+数值）后紧跟纯中文描述行（≥6字） → 拆分
        （针对同一页两个同结构表格用一句描述行分隔的场景）
    Tier 2（中置信度）：无年份/变化词 但 列结构突变 + 全短文本标签 + 前有多行数据 → 拆分
        （用于检测"本集团 本行""阶段一 阶段二"等无语境关键词的表头行）

    拆分出的子表独立识别自己的表头行，而非复用第一张表的。

    Returns:
        (tables, split_count)
    """
    result = []
    split_count = 0

    for table in tables:
        rows = table.get("rows", [])
        if len(rows) < 8:
            result.append(table)
            continue

        # 检测当前表中数据区的 items 列数（用于 Tier 2 列突变检测）
        data_items_counts = [
            len(r.get("items", [])) for r in rows[2:]
            if any(_is_numeric_cell(t) for t in r.get("texts", []))
        ]
        avg_data_items = (
            sum(data_items_counts) / len(data_items_counts)
            if data_items_counts else 0
        )

        # 检测第3行起是否有表头行特征
        split_positions = []
        for ri in range(2, len(rows) - 1):
            row = rows[ri]
            texts = row.get("texts", [])
            non_empty = [t.strip() for t in texts if t.strip()]

            if not non_empty:
                continue

            # 该行是表头的信号
            is_header = False

            # 首列通常是行标签/序号（如 "43"、"结算与清算手续费"），
            # 其数值本质上不是财务数据。排除首列后再判断数值占比，
            # 避免将小节编号误判为财务数值，导致标题行被当作数据行。
            data_cells = non_empty[1:] if len(non_empty) > 1 else non_empty

            # ------ Tier 1: 高置信度（保留原逻辑） ------

            # 信号1: 含年份词
            has_year = any(YEAR_PATTERN.search(t) for t in non_empty)

            # 信号2: 含变化/增减关键词
            has_delta = any(any(kw in t for kw in DELTA_KW) for t in non_empty)

            # 信号3: 数值少（< 30%的非空单元格是数值）
            # 注意：PDF 经常把 "2024年12月31日" 拆成 "2024" + "年12月31日"，
            # 纯年份数字会被 float() 误判为财务数值 → 跳过纯年份数字
            num_count = 0
            for t in data_cells:
                clean = t.replace(",", "").replace("%", "").replace(" ", "")
                # 排除纯四位年份数字（如 "2024"、"2023"），避免 PDF 拆分导致的误判
                if (len(clean) == 4 and clean.isdigit()
                        and 1900 <= int(clean) <= 2099):
                    continue
                try:
                    float(clean)
                    num_count += 1
                except ValueError:
                    pass
            is_mostly_text = num_count / max(len(data_cells), 1) < 0.3

            # Tier 1 综合判断
            # 常规：年份/变化词 + 文本为主 + ≥2 个非空单元格
            tier1_normal = (
                (has_year or has_delta) and is_mostly_text and len(non_empty) >= 2
            )
            # 特殊：单单元格年份分隔行（如 PDF 中居中渲染的 "2023年12月31日"）
            tier1_single_year = (
                has_year and not has_delta
                and len(non_empty) == 1 and is_mostly_text
            )

            if tier1_normal or tier1_single_year:
                # 再确认：该行之前有数据行特征
                # 注意：排除首列，与 Tier 1 自身逻辑一致（首列是行标签/序号，不算数据）
                prev_data = False
                start_check = max(0, ri - 5)  # 扩大到5行，应对分隔行+标题行占位
                for prev_ri in range(start_check, ri):
                    prev_texts = rows[prev_ri].get("texts", [])
                    data_cells = prev_texts[1:] if len(prev_texts) > 1 else prev_texts
                    if any(_is_numeric_cell(t) for t in data_cells):
                        prev_data = True
                        break

                if prev_data:
                    is_header = True

                    # 🔒 纯日期行护栏：只有年份信号（无变化词、无实体标签）、
                    # 前后数据行列结构一致 → 这是表内子表头，不拆分
                    if has_year and not has_delta:
                        has_entity = any(t in ENTITY_KW for t in non_empty)
                        if not has_entity:
                            # 收集前6行中数据行的列数
                            before_cols = []
                            for p_ri in range(max(0, ri - 6), ri):
                                p_texts = rows[p_ri].get("texts", [])
                                if any(_is_numeric_cell(t) for t in p_texts):
                                    before_cols.append(len(p_texts))
                            # 收集后4行中数据行的列数
                            after_cols = []
                            for n_ri in range(ri + 1, min(len(rows), ri + 5)):
                                n_texts = rows[n_ri].get("texts", [])
                                if any(_is_numeric_cell(t) for t in n_texts):
                                    after_cols.append(len(n_texts))
                            if before_cols and after_cols:
                                from collections import Counter as _Ctr
                                _bm = _Ctr(before_cols).most_common(1)[0][0]
                                _am = _Ctr(after_cols).most_common(1)[0][0]
                                if _bm == _am:
                                    if _DEBUG_MIXED_SPLIT:
                                        print(
                                            f"  [Tier1 Guard] ri={ri} "
                                            f"pure date row, col structure "
                                            f"consistent ({_bm}), NOT splitting"
                                        )
                                    is_header = False

            # ------ Tier 1.5: 标题描述行检测（增强版） ------
            # 场景：同一页内两个独立表格之间，可能没有完整的年份表头行
            # （如分隔文本 + 标题描述行），但标题描述行本身就能作为拆分信号。
            # 特征：纯文本、含长中文（≥10字）、前面紧邻数据行。
            #
            # 增强：分隔行通常只有 1-2 个 text_items（描述文本），
            # 而数据行的 items 数通常 ≥3。如果纯文本行的 items 数
            # 与数据行接近，说明该行文本被分到了多个列对齐的 item 中，
            # 更可能是表格内部的文本轴标签行，而非表格间分隔行。
            if not is_header:
                # 排除首列后检查是否为全文本（首列可能是小节编号）
                all_text = all(not _is_numeric_cell(t) for t in data_cells)
                cn_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t))
                               for t in non_empty)
                if all_text and cn_chars >= 10 and len(non_empty) >= 1:
                    # 确认该描述行之前有数据行（至少 2 行含数值）
                    prev_data_rows = 0
                    prev_items_counts = []  # 新增：收集数据行的 items 数
                    for prev_ri in range(max(0, ri - 5), ri):
                        prev_texts = rows[prev_ri].get("texts", [])
                        if sum(1 for t in prev_texts if _is_numeric_cell(t)) >= 1:
                            prev_data_rows += 1
                            prev_items_counts.append(
                                len(rows[prev_ri].get("items", []))
                            )
                    # 计算数据行的典型 items 数
                    typical_items = (
                        max(set(prev_items_counts), key=prev_items_counts.count)
                        if prev_items_counts else 0
                    )
                    cur_items = len(row.get("items", []))
                    # 如果当前行 items 数 ≥ 数据行典型 items 数的 60%，
                    # 说明文本分散在多列中，更像是表格内部行而非分隔行
                    items_too_wide = (
                        typical_items >= 2
                        and cur_items >= max(2, typical_items * 0.6)
                    )
                    if _DEBUG_MIXED_SPLIT:
                        print(f"  [Tier1.5] ri={ri} all_text={all_text} cn_chars={cn_chars} "
                              f"prev_data_rows={prev_data_rows} (need>=2) "
                              f"cur_items={cur_items} typical_items={typical_items} "
                              f"items_too_wide={items_too_wide} "
                              f"texts={non_empty[:2]}")
                    if prev_data_rows >= 2 and not items_too_wide:
                        is_header = True

            # ------ Tier 1.6: 合计行后的标题描述行检测 ------
            # 场景：合计/小计行（如"营业收入""利润总额"）后紧跟的纯中文描述行，
            # 例如"下表列出于所示期间本集团按区域划分的利润总额分布情况。"
            # 这是 PDF 表格之间最明确的拆分信号 —— 前表以合计行结束，
            # 后跟一段标题描述，紧接着是下一个同结构但不同指标的表格。
            if not is_header:
                # 排除首列后检查是否为全文本（首列可能是小节编号）
                all_text = all(not _is_numeric_cell(t) for t in data_cells)
                cn_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t))
                               for t in non_empty)
                if all_text and cn_chars >= 6 and len(non_empty) >= 1:
                    SUMMARY_WORDS = [
                        '合计', '总计', '总额', '营业收入', '利润总额',
                        '营业支出', '净利润', '综合收益', '营业利润',
                    ]
                    for prev_offset in range(1, min(4, ri + 1)):
                        prev_row = rows[ri - prev_offset]
                        prev_texts = prev_row.get("texts", [])
                        prev_non_empty = [t.strip() for t in prev_texts if t.strip()]
                        prev_joined = ''.join(prev_non_empty)
                        is_summary = any(kw in prev_joined for kw in SUMMARY_WORDS)
                        has_num = any(_is_numeric_cell(t) for t in prev_non_empty)
                        if is_summary and has_num:
                            if _DEBUG_MIXED_SPLIT:
                                print(f"  [Tier1.6] ri={ri} cn_chars={cn_chars} "
                                      f"prev_offset={prev_offset} "
                                      f"matched_kw={[kw for kw in SUMMARY_WORDS if kw in prev_joined]} "
                                      f"has_num={has_num} texts={non_empty[:2]}")
                            is_header = True
                            break

            # ------ Tier 1.7: 编号+短标题分隔行检测 ------
            # 场景："31  预计负债" —— 前表以"合计"结束，紧跟一个短的章节编号+标题，
            # 然后是下一个同结构但不同指标的表格。
            # 与 Tier 1.6（cn_chars ≥ 6）的区别：
            #   - 降低中文字符阈值至 ≥ 4，覆盖短标题如"预计负债""递延所得税""雇员福利"等
            #   - 非空单元格 ≤ 2（编号+标题，而非宽行数据）
            if not is_header:
                all_text = all(not _is_numeric_cell(t) for t in data_cells)
                cn_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t))
                               for t in non_empty)
                # 中文 ≥ 4 且非空单元格数 ≤ 2（编号+短标题）
                if all_text and cn_chars >= 4 and 1 <= len(non_empty) <= 2:
                    SUMMARY_WORDS = [
                        '合计', '总计', '总额', '营业收入', '利润总额',
                        '营业支出', '净利润', '综合收益', '营业利润',
                    ]
                    for prev_offset in range(1, min(4, ri + 1)):
                        prev_row = rows[ri - prev_offset]
                        prev_texts = prev_row.get("texts", [])
                        prev_non_empty = [t.strip() for t in prev_texts if t.strip()]
                        prev_joined = ''.join(prev_non_empty)
                        is_summary = any(kw in prev_joined for kw in SUMMARY_WORDS)
                        has_num = any(_is_numeric_cell(t) for t in prev_non_empty)
                        if is_summary and has_num:
                            # 额外检查：分隔行 non_empty ≤ 2 确保不是宽数据行
                            if _DEBUG_MIXED_SPLIT:
                                print(f"  [Tier1.7] ri={ri} cn_chars={cn_chars} "
                                      f"prev_offset={prev_offset} non_empty={len(non_empty)} "
                                      f"matched_kw={[kw for kw in SUMMARY_WORDS if kw in prev_joined]} "
                                      f"has_num={has_num} texts={non_empty[:2]}")
                            is_header = True
                            break

            # ------ Tier 2: 中置信度（新增：检测无年份/变化词的表头） ------
            if not is_header and not has_year and not has_delta:
                n_items = len(row.get("items", []))

                # 条件A: 列结构突变 — items 数比前面数据区少 ≥3
                items_drop = (
                    avg_data_items > 0
                    and (avg_data_items - n_items) >= 3
                )

                # 条件B: 排除首列后全部是纯文本（0个财务数值单元格）
                all_text_cells = all(not _is_numeric_cell(t) for t in data_cells)

                # 条件C: 所有非空单元格都含中文且 ≤10 字符（像表头标签）
                all_short_chinese = all(
                    len(t) <= 10 and bool(re.search(r'[\u4e00-\u9fff]', t))
                    for t in non_empty
                )

                # 条件D: 前面至少 3 行含数值数据（确认前面是数据区）
                prev_numeric_rows = 0
                for prev_ri in range(max(0, ri - 8), ri):
                    prev_texts = rows[prev_ri].get("texts", [])
                    if sum(1 for t in prev_texts if _is_numeric_cell(t)) >= 2:
                        prev_numeric_rows += 1

                if (
                    items_drop
                    and all_text_cells
                    and all_short_chinese
                    and prev_numeric_rows >= 3
                    and n_items >= 2
                ):
                    is_header = True

            # 🔒 统一标签续行检查（后置 Guard）
            # 当任何 Tier 判定 is_header=True 但没有年份信号时，
            # 检查下一行是否是"中文标签 + 数值"的续行模式。
            # 场景：PDF 将长标签拆分为两行，上半行（含"变动"等关键词）
            # 被 Tier 1 误判为表头拆分点。此 Guard 检测下半行的续行模式
            # 并撤销误判。
            # 典型场景：
            #   "...以公允价值计量且其变动计入其他综合"  ← 被 Tier 1 误判为表头
            #   "收益的金融资产损失准备  (2,754)  –  –  (2,754)"  ← 续行
            if is_header and not has_year:
                for next_ri in range(ri + 1, min(len(rows), ri + 3)):
                    next_texts = rows[next_ri].get("texts", [])
                    next_ne = [t.strip() for t in next_texts if t.strip()]
                    if not next_ne:
                        continue
                    first_cn = (
                        len(re.findall(r'[\u4e00-\u9fff]', next_ne[0])) >= 2
                        if next_ne else False
                    )
                    rest_num = (
                        any(_is_numeric_cell(t) for t in next_ne[1:])
                        if len(next_ne) >= 2 else False
                    )
                    # 第一格是中文标签 + 其余格含数值 + ≥3个非空格 → 续行模式
                    if first_cn and rest_num and len(next_ne) >= 3:
                        if _DEBUG_MIXED_SPLIT:
                            print(
                                f"  [POST-GUARD] ri={ri} "
                                f"下一行是标签续行 "
                                f"(first_cn={first_cn} rest_num={rest_num})"
                                f" → 撤销拆分"
                            )
                        is_header = False
                        break

            # ---- 结构连续性守卫（增强版） ----
            # 非年份/变化词触发的拆分（Tier 1.5 / Tier 1.6 / Tier 2），
            # 需验证拆分点前后的数据行是否沿用相同的列结构。
            # 如果列数一致 → 这是同一表格内的连续数据区域，撤销拆分。
            # 例外：年份行 / 变化词拆分是天然可靠的超级信号，不在此守卫管辖范围。
            #
            # 增强策略（三级递进）：
            #   L1 — 众数相同：前后数据行的最常见列数完全一致 → 强信号，直接撤销
            #   L2 — 平均值近 + 首列标签风格一致：两段都使用中文标签首列 → 撤销
            #   L3 — 仅平均值近：保守保留原行为
            if is_header and not has_year and not has_delta:
                before_col_counts = []
                before_first_labels = []  # 新增：收集首列标签文本
                for prev_ri in range(max(0, ri - 5), ri):
                    prev_texts = rows[prev_ri].get("texts", [])
                    if any(_is_numeric_cell(t) for t in prev_texts):
                        before_col_counts.append(
                            len(rows[prev_ri].get("items", []))
                        )
                        if prev_texts and prev_texts[0].strip():
                            before_first_labels.append(prev_texts[0].strip())

                after_col_counts = []
                after_first_labels = []
                for next_ri in range(ri + 1, min(len(rows), ri + 6)):
                    next_texts = rows[next_ri].get("texts", [])
                    if any(_is_numeric_cell(t) for t in next_texts):
                        after_col_counts.append(
                            len(rows[next_ri].get("items", []))
                        )
                        if next_texts and next_texts[0].strip():
                            after_first_labels.append(next_texts[0].strip())

                if before_col_counts and after_col_counts:
                    before_avg = (
                        sum(before_col_counts) / len(before_col_counts)
                    )
                    after_avg = (
                        sum(after_col_counts) / len(after_col_counts)
                    )
                    avg_near = abs(before_avg - after_avg) <= 1

                    # L1: 众数相同 → 强信号，直接撤销
                    mode_same = False
                    if len(before_col_counts) >= 2 and len(after_col_counts) >= 2:
                        before_mode = Counter(before_col_counts).most_common(1)[0][0]
                        after_mode = Counter(after_col_counts).most_common(1)[0][0]
                        mode_same = before_mode == after_mode

                    # L2: 首列中文标签风格一致性
                    labels_consistent = False
                    if before_first_labels and after_first_labels:
                        def _has_cn(s):
                            return bool(re.search(r'[\u4e00-\u9fff]', s))
                        before_cn = sum(1 for t in before_first_labels if _has_cn(t))
                        after_cn = sum(1 for t in after_first_labels if _has_cn(t))
                        # 两侧 ≥50% 的首列都是中文标签
                        labels_consistent = (
                            before_cn >= max(1, len(before_first_labels) * 0.5)
                            and after_cn >= max(1, len(after_first_labels) * 0.5)
                        )

                    if mode_same:
                        if _DEBUG_MIXED_SPLIT:
                            print(
                                f"  [GUARD-L1] ri={ri} 众数相同 "
                                f"(mode={before_mode})"
                                f" → 撤销拆分"
                            )
                        is_header = False
                    elif avg_near and labels_consistent:
                        if _DEBUG_MIXED_SPLIT:
                            print(
                                f"  [GUARD-L2] ri={ri} 结构连续+标签一致 "
                                f"(before_avg={before_avg:.1f} "
                                f"after_avg={after_avg:.1f})"
                                f" → 撤销拆分"
                            )
                        is_header = False
                    elif avg_near:
                        if _DEBUG_MIXED_SPLIT:
                            print(
                                f"  [GUARD-L3] ri={ri} 结构连续 "
                                f"(before_avg={before_avg:.1f} "
                                f"after_avg={after_avg:.1f})"
                                f" → 撤销拆分"
                            )
                        is_header = False

            if is_header:
                split_positions.append(ri)
                LOG = _get_log()
                LOG.debug(
                    "  [混合表检测] page=%d tid=%s 发现拆分点 ri=%d "
                    "text=%s items=%d",
                    table.get("page", 0),
                    table.get("table_id", "?"),
                    ri,
                    " ".join(row.get("texts", []))[:60],
                    len(row.get("items", [])),
                )

        if _DEBUG_MIXED_SPLIT and split_positions:
            print(f"  [MIXED-SPLIT DEBUG] table rows={len(rows)} "
                  f"split_positions={split_positions}")

        if not split_positions:
            result.append(table)
            continue

        # 去重：连续多行都是表头特征 → 合并为一个拆分点
        merged_splits = []
        last_split = -99
        for sp in sorted(split_positions):
            if sp - last_split > 2:  # 至少间隔2行
                merged_splits.append(sp)
            last_split = sp
        split_positions = merged_splits

        if not split_positions:
            result.append(table)
            continue

        # 执行切分
        segments = []
        prev_split = 0
        for sp in split_positions:
            if sp - prev_split >= 3:  # 至少3行才有意义
                segments.append((prev_split, sp))
            prev_split = sp
        if len(rows) - prev_split >= 3:
            segments.append((prev_split, len(rows)))

        if len(segments) <= 1:
            result.append(table)
            continue

        LOG = _get_log()
        LOG.info(
            "  [混合表拆分] page=%d tid=%s total_rows=%d → %d segments: %s",
            table.get("page", 0), table.get("table_id", "?"),
            len(rows), len(segments),
            ", ".join(f"[{s},{e})" for s, e in segments),
        )

        # 为每个 segment 创建子表（各自独立识别表头）
        for seg_idx, (start, end) in enumerate(segments):
            seg_rows = rows[start:end]

            # 每个 segment 独立检测自己的表头（不用模板复用）
            header_end = _detect_header_end_for_segment(seg_rows)
            if header_end > 0 and len(seg_rows) > header_end + 2:
                final_rows = seg_rows
            else:
                final_rows = seg_rows

            col_ranges = _estimate_column_x_ranges(final_rows)
            col_schema = infer_column_schema(final_rows)

            # 用子表自己的列结构重新归一化 rows，修复混合表拆分后
            # 列数不匹配导致的 text 错位问题
            final_rows = _normalize_rows_to_columns(final_rows, col_ranges)

            # 重建 items
            all_items = []
            for r in final_rows:
                all_items.extend(r.get("items", []))

            y0_val = 0
            y1_val = 0
            if start < len(rows) and rows[start].get("items"):
                y0_val = min(it["y0"] for it in rows[start].get("items", [{"y0": 0}]))
            if end > 0 and rows[end - 1].get("items"):
                y1_val = max(it["y1"] for it in rows[end - 1].get("items", [{"y1": 0}]))

            sub_table = {
                "page": table["page"],
                "pages": [table["page"]],
                "y0": y0_val,
                "y1": y1_val,
                "text_items": all_items,
                "rows": final_rows,
                "row_count": len(final_rows),
                "is_cross_page": False,
                "caption": _get_segment_caption(table.get("caption", ""), final_rows, seg_idx, len(segments)),
                "region_index": table.get("region_index", -1),
                "confidence": table.get("confidence", 0),
                "column_x_ranges": col_ranges,
                "column_schema": col_schema,
                "is_split_from_mixed": True,
                "original_table_id": table.get("table_id", -1),
            }
            result.append(sub_table)
            split_count += 1

            LOG.debug(
                "  [子表] seg=%d rows=%d cols=%d capt='%s' col_ranges=%s schema=%s",
                seg_idx, len(final_rows), len(col_ranges),
                sub_table["caption"][:30],
                ", ".join(f"[{x0:.0f},{x1:.0f}]" for x0, x1 in col_ranges),
                ", ".join(cs.get("dominant_type", "?") for cs in col_schema),
            )

    return result, split_count


def _detect_header_end_for_segment(seg_rows: List[dict]) -> int:
    """检测一个 segment 中表头区域的结束位置。

    返回第一个数据行的索引（即表头行数）。
    使用连续 ≥2 行含数值数据的规则。
    """
    if len(seg_rows) < 3:
        return 0

    consecutive_numeric = 0
    for ri, row in enumerate(seg_rows):
        texts = row.get("texts", [])
        numeric_count = sum(1 for t in texts if _is_numeric_cell(t))
        if numeric_count >= 2:
            consecutive_numeric += 1
            if consecutive_numeric >= 2:
                return max(0, ri - 1)  # 返回表头结束位置
        else:
            consecutive_numeric = 0

    return 0


def _is_numeric_cell(text: str) -> bool:
    """检查单元格是否为数值。"""
    if not text or not text.strip():
        return False
    s = text.strip().rstrip('%').replace(',', '').replace(' ', '')
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


# ---- (3) 相邻相关表格合并 ----

def _merge_adjacent_related_tables(tables: List[dict]) -> Tuple[List[dict], int]:
    """检测并合并同一页内的相邻相关表格（续表）。

    判断标准：
    1. 两张表在同一页
    2. 后表的第一行没有表头特征（出现数值类数据）
    3. 列结构相似

    Returns:
        (tables, merge_count)
    """
    if len(tables) < 2:
        return tables, 0

    merged_pairs = []  # [(keep_idx, remove_idx), ...]

    for idx in range(len(tables) - 1):
        table_a = tables[idx]
        table_b = tables[idx + 1]
        same_family = False  # 是否同一次混合表拆分的两张子表

        # 必须同页
        pg_a = table_a.get("page", 0)
        pg_b = table_b.get("page", 0)
        if pg_a != pg_b:
            continue

        # 已经是跨页表的不再合并
        if table_a.get("is_cross_page") or table_b.get("is_cross_page"):
            continue

        # ═══════════════════════════════════════════════════════════════
        # 高置信度合并：相邻表格，中间无纯文本行，后表无表头，列数完全相同
        # 三个条件全满足 → 直接合并（优先级最高，跳过所有其他检查）
        # ═══════════════════════════════════════════════════════════════
        rows_a = table_a.get("rows", [])
        rows_b = table_b.get("rows", [])

        y_gap = table_b.get("y0", 0) - table_a.get("y1", 0)

        # 条件1: 中间没有纯文本行 —— Y 间距不足以容纳一行文本
        # 基于表A的实际行高计算阈值（中位数行高 × 2）
        if rows_a:
            row_heights = []
            for row in rows_a:
                items = row.get("items", [])
                if items:
                    h = max(it.get("y1", 0) - it.get("y0", 0) for it in items)
                    if h > 0:
                        row_heights.append(h)
            if row_heights:
                row_heights.sort()
                median_row_h = row_heights[len(row_heights) // 2]
            else:
                median_row_h = 12.0
        else:
            median_row_h = 12.0
        max_gap_for_no_text = max(median_row_h * 2.0, 20.0)
        no_text_between = y_gap < max_gap_for_no_text

        if no_text_between:
            # 条件2: 后表没有独立完整表头
            if not _has_complete_table_header(table_b):
                # 条件3: 有效列数完全相同
                # 注意：用 rows 的实际 text 列数而非 column_x_ranges
                # 因为 _merge_sparse_columns_by_rows 可能在小表格上
                # 误将有效列合并为稀疏列（如仅前2行有数据的5列表格
                # 被压缩为2列），导致 column_x_ranges 失真
                if rows_a:
                    cols_a = max(len(r.get("texts", [])) for r in rows_a)
                else:
                    cols_a = 0
                if rows_b:
                    cols_b = max(len(r.get("texts", [])) for r in rows_b)
                else:
                    cols_b = 0

                if cols_a > 1 and cols_a == cols_b:
                    merged_pairs.append((idx, idx + 1))
                    continue  # 高置信度合并，跳过后续所有检查

        # ═══════════════════════════════════════════════════════════════
        # 子表头行间隙合并：Y间隙被日期/实体子表头行撑大，但后表仍是续表
        #
        # 场景：PDF 渲染时，日期行（2024年  2023年）或实体标签行
        # （本集团 / 本行）占据一行垂直空间，导致 Y 间隙 > 2×行高，
        # no_text_between 失败。但这些行本质上是表头的一部分，
        # 不应被当作"文本屏障"阻止合并。
        #
        # 判断标准：
        #   ① Y间隙 < 5×行高（远小于文本段落间隔）
        #   ② 后表首行是纯日期行（≥2年份、无entity）或单实体标签行
        #   ③ 后表后续行中有数据行
        #   ④ 后表没有独立完整表头（年份+entity双全）
        #   ⑤ 列数完全相同
        # ═══════════════════════════════════════════════════════════════
        subheader_gap = max(median_row_h * 5.0, 36.0)

        if not no_text_between and y_gap < subheader_gap and rows_b:
            first_texts_b = rows_b[0].get("texts", [])
            first_ne = [t.strip() for t in first_texts_b if t.strip()]

            if first_ne:
                yr_cnt = sum(1 for t in first_ne if YEAR_PATTERN.search(t))
                has_ent = any(t in ENTITY_KW for t in first_ne)
                is_dual_ent = _is_dual_entity_header_row(first_ne)

                is_subheader = False
                # 纯日期行：≥2个年份单元格，无entity标签
                if yr_cnt >= 2 and not has_ent:
                    is_subheader = True
                # 单实体标签行：恰好1个entity标签（非双实体表头行）
                elif has_ent and not is_dual_ent and yr_cnt < 2:
                    is_subheader = True

                if is_subheader:
                    # 确认后表第2~5行中有至少一个数据行
                    has_data_after = False
                    for ck in range(1, min(len(rows_b), 5)):
                        ck_texts = rows_b[ck].get("texts", [])
                        if any(_is_numeric_cell(t) for t in ck_texts):
                            has_data_after = True
                            break

                    if has_data_after:
                        # 确认后表没有独立完整表头（子表头不算完整表头）
                        if not _has_complete_table_header(table_b):
                            sub_cols_a = (
                                max(len(r.get("texts", [])) for r in rows_a)
                                if rows_a else 0
                            )
                            sub_cols_b = (
                                max(len(r.get("texts", [])) for r in rows_b)
                                if rows_b else 0
                            )
                            if sub_cols_a > 1 and sub_cols_a == sub_cols_b:
                                merged_pairs.append((idx, idx + 1))
                                continue

        # is_split_from_mixed 处理：同一父表拆出的两张表，仍需走完整表头检测
        # 避免把"阶段一/阶段二"误拆合并，也避免把独立表格（如不同指标分类的财务表）错误合并
        a_from_split = table_a.get("is_split_from_mixed", False)
        b_from_split = table_b.get("is_split_from_mixed", False)

        if a_from_split or b_from_split:
            # 只有一方是拆分产物 → 不相关，跳过
            if a_from_split != b_from_split:
                continue
            # 双方都来自混合拆分 → 检查是否同一父表
            a_orig = table_a.get("original_table_id", -1)
            b_orig = table_b.get("original_table_id", -1)
            if a_orig != b_orig or a_orig == -1:
                continue

            # 双方来自同一次混合表拆分 → 标记，后续列数不同则拒绝合并
            same_family = True

        # 检查后表完整表头（多行扫描）：独立完整表格不合并
        if _has_complete_table_header(table_b):
            continue

        # 检查后表第一行是否数据行
        if not rows_b:
            continue

        first_texts = rows_b[0].get("texts", [])
        non_empty = [t.strip() for t in first_texts if t.strip()]

        if not non_empty:
            continue

        is_likely_data_row = _is_first_row_data_row(first_texts, rows_b)
        if not is_likely_data_row:
            continue

        # 列结构相似
        cols_a = len(table_a.get("column_x_ranges", []))
        cols_b = len(table_b.get("column_x_ranges", []))
        if cols_a == 0 or cols_b == 0:
            if rows_a:
                cols_a = max(len(r.get("texts", [])) for r in rows_a)
            if rows_b:
                cols_b = max(len(r.get("texts", [])) for r in rows_b)

        if cols_a > 0 and cols_b > 0:
            col_diff = abs(cols_a - cols_b)
            if same_family and col_diff > 0:
                # 同一次混合拆分的两张子表、列数不同 → 不同类型表格，拒绝合并
                # 宁可漏合（两张表分开）不可错合（数据塞错列）
                continue
            if col_diff <= 2:
                merged_pairs.append((idx, idx + 1))

    if not merged_pairs:
        return tables, 0

    # 执行合并（从后往前）
    consumed = set()
    result = []
    merge_count = 0

    merged_pairs.sort(key=lambda x: x[0])
    i = 0
    while i < len(tables):
        if i in consumed:
            i += 1
            continue

        keeper = tables[i]
        j = i
        chain = [i]

        next_j = j + 1
        while next_j < len(tables):
            if (j, next_j) in [(a, b) for a, b in merged_pairs]:
                chain.append(next_j)
                consumed.add(next_j)
                j = next_j
                next_j = j + 1
                merge_count += 1
            else:
                break

        if len(chain) > 1:
            for merged_idx in chain[1:]:
                keeper = _concat_adjacent_table(keeper, tables[merged_idx])

        result.append(keeper)
        i = j + 1 if len(chain) > 1 else i + 1

    return result, merge_count


def _is_first_row_data_row(texts: List[str], rows: List[dict]) -> bool:
    """判断后表的第一行是否更像数据行（而非表头行）。

    数据行特征：至少包含一个明显的数值（含千分位逗号、小数点），
    且该行看起来不像表头（无年份词 + 变化/增减关键词）。
    """
    if not texts:
        return False

    non_empty = [t.strip() for t in texts if t.strip()]
    if not non_empty:
        return False

    # 检查是否是明显的表头
    year_pattern = re.compile(r'(19|20)\d{2}')
    has_year = any(year_pattern.search(t) for t in non_empty)
    delta_kw = ['变化', '增减', '变动', '增幅', '项目', '指标', '单位']
    has_delta = any(any(kw in t for kw in delta_kw) for t in non_empty)

    if has_year and has_delta:
        return False  # 明显是表头

    # 检查是否有长数字
    has_long_number = any(
        re.search(r'\d[\d,.]{3,}', t) for t in non_empty
    )

    # 检查该行有多列数值
    num_cells = sum(1 for t in non_empty if _is_numeric_cell(t))
    num_ratio = num_cells / max(len(non_empty), 1)

    # 多列数值 → 数据行
    if num_ratio >= 0.5 and num_cells >= 2:
        return True

    # 有长数字（如财务数据）→ 数据行
    if has_long_number and not (has_year and has_delta):
        return True

    # 纯中文短标签 → 表头
    cn_chars = sum(len(re.findall(r'[\u4e00-\u9fff]', t)) for t in non_empty)
    total_len = sum(len(t) for t in non_empty)
    cn_ratio = cn_chars / max(total_len, 1)

    if cn_ratio >= 0.6 and not has_long_number:
        return False  # 中文为主 → 表头

    return num_cells >= 1  # 至少有一列是数值


def _concat_adjacent_table(table_a: dict, table_b: dict) -> dict:
    """将同页相邻的 table_b 拼接到 table_a 后面。"""
    rows_a = table_a.get("rows", [])
    rows_b = table_b.get("rows", [])

    all_rows = list(rows_a) + list(rows_b)
    all_items = list(table_a.get("text_items", [])) + list(table_b.get("text_items", []))

    col_ranges = _estimate_column_x_ranges(all_rows)
    col_schema = infer_column_schema(all_rows)

    return {
        "page": table_a["page"],
        "pages": table_a.get("pages", [table_a["page"]]),
        "y0": table_a["y0"],
        "y1": table_b["y1"],
        "text_items": all_items,
        "rows": all_rows,
        "row_count": len(all_rows),
        "is_cross_page": table_a.get("is_cross_page", False),
        "caption": table_a.get("caption", "") or table_b.get("caption", ""),
        "region_index": table_a.get("region_index", -1),
        "confidence": table_a.get("confidence", 0),
        "column_x_ranges": col_ranges,
        "column_schema": col_schema,
        "is_merged_adjacent": True,
    }


# ---- (4) 描述文本边界增强 ----

def _enhance_with_caption_boundary(tables: List[dict]) -> List[dict]:
    """利用表格上方描述文本增强边界识别。

    表格上方的描述文本（如"单位：百万元"、"截至2024年12月31日"等）
    可以作为表格分界的重要参考。这个函数将这些描述文本信息
    记录到表格的 metadata 中，辅助后续的人工验证。
    """
    for table in tables:
        caption = table.get("caption", "").strip()
        if not caption:
            continue

        # 检测 caption 是否包含表格范围描述
        range_kw = ["截至", "截止", "单位：", "Unit:", "万元", "千元", "百万元",
                     "财务数据", "经营业绩", "主要指标", "资产负债表", "利润表", "现金流量"]
        has_range_info = any(kw in caption for kw in range_kw)

        # 记录边界增强信息
        table["caption_info"] = {
            "text": caption,
            "has_table_range_info": has_range_info,
            "can_be_boundary": has_range_info,  # 含范围信息的描述可作边界参考
        }

        # 如果 caption 含范围信息，记录到 table 的 metadata
        if has_range_info:
            table["description_text"] = caption

    return tables


# ---- (5) 完整表格识别 + 真表格标记 ----

def _classify_table_quality(tables: List[dict]) -> List[dict]:
    """对每张表进行分类：是否为完整表格，是否为真表格。

    综合评估维度：
    - 是否有表头（前2行含年份词/中文标签）
    - 是否有数值数据列
    - 表格行数是否合理
    - 列数是否 ≥ 2
    - 是否为目录/图表等非表格

    输出字段：
    - is_real_table: 是否为真财务表格
    - is_complete: 表格是否完整
    - table_category: 分类标签
    - has_header: 是否有表头
    - has_numeric_data: 是否有数值数据
    """
    for table in tables:
        rows = table.get("rows", [])
        if not rows:
            table["is_real_table"] = False
            table["is_complete"] = False
            table["table_category"] = "空表"
            table["has_header"] = False
            table["has_numeric_data"] = False
            continue

        # --- 检测表头（动态深度：扫描前 1/4 行或最多 6 行） ---
        has_header = False
        header_scan_limit = max(2, min(6, len(rows) // 4))  # 至少2行，最多6行或 1/4 行数
        for ri in range(min(header_scan_limit, len(rows))):
            texts = rows[ri].get("texts", [])
            if _is_header_like(texts):
                has_header = True
                break
            # 额外检查：含统一表头关键词 + 多列
            all_text = "".join(texts)
            if any(kw in all_text for kw in HEADER_KW) and len(texts) >= 2:
                has_header = True
                break

        table["has_header"] = has_header

        # --- 检测数值数据 ---
        max_cols = max((len(r.get("texts", [])) for r in rows), default=0)
        has_numeric_data = False
        numeric_col_count = 0

        # 动态计算实际表头行数（只跳过真正被识别为表头的行，避免误吞数据行）
        if has_header:
            header_skip = 1
            for ri in range(1, min(header_scan_limit, len(rows))):
                texts = rows[ri].get("texts", [])
                if _is_header_like(texts) or (
                    any(kw in "".join(texts) for kw in HEADER_KW) and len(texts) >= 2
                ):
                    header_skip += 1
                else:
                    break
        else:
            header_skip = 1

        total_data_rows = max(0, len(rows) - header_skip)
        # 数据行 ≤ 2 → 降阈值为 1（小表格只有 1~2 行数据时不应被判为"文本列表"）
        min_numeric_threshold = 1 if total_data_rows <= 2 else 2

        if max_cols >= 2:
            for c in range(max_cols):
                col_vals = []
                for ri, row in enumerate(rows):
                    texts = row.get("texts", [])
                    if c < len(texts) and ri >= header_skip:
                        val = texts[c].strip()
                        if val:
                            col_vals.append(val)
                if not col_vals:
                    continue
                numeric_in_col = sum(1 for v in col_vals if _is_numeric_cell(v))
                ratio = numeric_in_col / max(len(col_vals), 1)
                if ratio >= 0.5 and numeric_in_col >= min_numeric_threshold:
                    numeric_col_count += 1
                    has_numeric_data = True

        table["has_numeric_data"] = has_numeric_data
        table["numeric_col_count"] = numeric_col_count

        # --- 综合判定 ---
        row_count = len(rows)
        col_count = max_cols

        # is_real_table: 有数值数据 + 列数≥2 + 行数≥3
        is_real_table = has_numeric_data and col_count >= 2 and row_count >= 3
        table["is_real_table"] = is_real_table

        # is_complete: 有表头 + 有数据 + 行数≥3
        is_complete = has_header and row_count >= 3
        table["is_complete"] = is_complete

        # table_category
        if not is_real_table:
            if any("目" in t for row in rows[:3] for t in row.get("texts", []) if "目录" in t):
                table["table_category"] = "目录"
            elif numeric_col_count == 0:
                # 回退检查：has_header 且 header_skip > 1 时，数据行可能被过度跳过
                # 尝试只用 header_skip=1 重新检测数值，避免误吞紧接表头的数据行
                if has_header and header_skip > 1 and max_cols >= 2:
                    fb_numeric_col_count = 0
                    for c in range(max_cols):
                        col_vals_fb = []
                        for ri, row in enumerate(rows):
                            texts = row.get("texts", [])
                            if c < len(texts) and ri >= 1:
                                val = texts[c].strip()
                                if val:
                                    col_vals_fb.append(val)
                        if not col_vals_fb:
                            continue
                        n_fb = sum(1 for v in col_vals_fb if _is_numeric_cell(v))
                        if n_fb >= 1 and n_fb / len(col_vals_fb) >= 0.3:
                            fb_numeric_col_count += 1
                    if fb_numeric_col_count > 0:
                        table["table_category"] = "非标准表格"
                        table["has_numeric_data"] = True
                        table["numeric_col_count"] = fb_numeric_col_count
                        table["is_real_table"] = True
                    else:
                        table["table_category"] = "文本列表"
                else:
                    table["table_category"] = "文本列表"
            else:
                table["table_category"] = "非标准表格"
        elif is_complete:
            table["table_category"] = "财务数据表"
        else:
            table["table_category"] = "数据表(缺表头)"

        # 记录分类详情
        table["quality_checks"] = {
            "has_header": has_header,
            "has_numeric_data": has_numeric_data,
            "numeric_col_count": numeric_col_count,
            "row_count": row_count,
            "col_count": col_count,
        }

    return tables


# ---- (6) 表格边界精细化处理（去尾/去头/跨页检测） ----

def _refine_table_boundaries(tables: List[dict]) -> List[dict]:
    """精细化表格边界处理（纯规则驱动）。

    仅针对"财务数据表"和"数据表(缺表头)"两类进一步判断并处理：

    规则1：底部尾巴行清理
      - 从表格底部向上扫描，检测连续的非数据行
      - 若倒数连续 ≥2 行没有任何数值单元格 → 全部移除
      - 汇总行（含"合计""总计"等关键词）不会被移除
      - 无论如何不会删到只剩 2 行（至少保留表头+1行数据）

    规则2：跨页续表检测
      - 对于"数据表(缺表头)"，检查与前一张表的列数是否匹配
      - 若列数相近（差异 ≤2）→ 标记 _cross_page_candidate = True

    规则3：表前非数据文本行清理
      - 从表格顶部向下扫描，移除表格数据行之前的多余文本行
      - 保留表头行（多列、年份关键词、短标签）不删除
      - 单位信息行（如"单位：万元"）会被移除，单位信息补充到 caption 中
      - 检测到表头特征行（多列+年份/变化关键词）时停止清理

    重新分类：行数变化后按需重新评估表格类型。
    """
    for idx, table in enumerate(tables):
        rows = table.get("rows", [])
        if not rows or len(rows) < 3:
            continue

        category = table.get("table_category", "")

        # 只处理财务数据表和缺表头的数据表
        if category not in ("财务数据表", "数据表(缺表头)"):
            continue

        modified = False

        # ---- 规则3：表前非数据文本行清理（先做，因为可能影响判定） ----
        header_start_idx = _find_first_table_header_or_data_row(rows)
        if header_start_idx > 0:
            removed = _strip_leading_text_rows(table, rows, header_start_idx)
            if removed:
                rows = table["rows"]
                modified = True

        if len(rows) < 3:
            continue

        # ---- 规则1a：尾部脚注/注释行清理（需要先于列签名清理执行） ----
        removed = _strip_tail_annotation_rows(table, rows)
        if removed:
            rows = table["rows"]
            modified = True

        if len(rows) < 3:
            continue

        # ---- 规则1b：底部尾巴行清理（列签名匹配） ----
        removed = _strip_tail_non_data_rows(table, rows)
        if removed:
            rows = table["rows"]
            modified = True

        if len(rows) < 2:
            continue

        # ---- 重新分类（若有行变动） ----
        if modified:
            _reclassify_single_table(table)

        # ---- 规则2：跨页续表检测 ----
        if table.get("table_category", "") == "数据表(缺表头)" and idx > 0:
            _mark_cross_page_candidate_if_match(tables, idx - 1, idx)

    return tables


# ================================================================
# 6.5 表头完整性检查与恢复
# ================================================================

def _recover_missing_headers(tables: List[dict]) -> List[dict]:
    """恢复被前一表格 strip 掉的主表头行。

    每个表格扫描前几行，检测缺失的表头类型：
    - 年份表头：≥2 个单元格含年份（如 2024年12月31日）
    - 实体表头：含实体标签（本集团/本行/本公司等）

    若缺失任一类型 → 向前回溯（同页 + 跨页）→ 全部恢复。
    """
    recovered_count = 0

    for idx in range(1, len(tables)):
        table = tables[idx]
        rows = table.get("rows", [])
        if not rows or len(rows) < 2:
            continue

        data_cols = _estimate_data_column_count(table)
        if data_cols < 2:
            continue

        # ---- 步骤1：扫描前几行，确定已有/缺失的表头类型 ----
        has_year, has_entity = _scan_header_types(rows, data_cols)

        # 两种都有 → 完整，跳过
        if has_year and has_entity:
            continue

        # ---- 步骤2：回溯搜索缺失的主表头 ----
        found_headers = _find_missing_headers_in_previous(
            tables, idx, data_cols,
            need_year=not has_year,
            need_entity=not has_entity,
        )

        if not found_headers:
            continue

        # ---- 步骤3：全部 prepend 到当前表 ----
        new_rows = list(rows)
        # found_headers 是按从近到远排列的，需要反转后 prepend
        for header_items in reversed(found_headers):
            year_row = _build_row_dict(header_items)
            new_rows = [year_row] + new_rows

        _rebuild_table_from_rows(table, new_rows)
        _reclassify_single_table(table)
        recovered_count += 1

    if recovered_count:
        print(f"  [表头恢复] 恢复了 {recovered_count} 张表格的丢失主表头")

    return tables


def _scan_header_types(rows: List[dict], data_cols: int) -> Tuple[bool, bool]:
    """扫描表的前几行，判断已有年份表头和实体表头。

    只扫描表头区域：遇到第一个数据行（含数值）即停止。
    返回 (has_year, has_entity)。
    """
    has_year = False
    has_entity = False

    for ri in range(min(len(rows), 6)):
        texts = rows[ri].get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue

        # 年份检测
        year_count = sum(1 for t in non_empty if YEAR_PATTERN.search(t))
        if year_count >= 2:
            has_year = True

        # 实体检测
        if any(t in ENTITY_KW for t in non_empty):
            has_entity = True

        # 碰到数据行（含数值且不全是 header keyword）→ 停止扫描
        if _is_likely_data_row(texts, non_empty):
            break

    return has_year, has_entity


def _is_likely_data_row(texts: List[str], non_empty: List[str]) -> bool:
    """判断一行是否为数据行（含数值，不是表头）。"""
    if not non_empty:
        return False
    # 双实体表头行（"本集团"+"本行"等同时出现）→ 绝不是数据行
    if _is_dual_entity_header_row(non_empty):
        return False
    # 排除划线占位符（–、—、- 等表示"无数据/不适用"的单元格）
    # 避免占位符膨胀分母导致多数值条件失效
    meaningful = [t for t in non_empty if not DASH_ONLY.match(t)]
    if not meaningful:
        return False
    # 至少一个单元格含数值特征
    numeric_count = sum(1 for t in meaningful if _is_cell_numeric_like(t))
    # 如果多数单元格含数值 → 这是数据行
    if numeric_count >= max(1, len(meaningful) // 2):
        return True
    return False


def _estimate_data_column_count(table: dict) -> int:
    """估算表格数据区的典型 items 数。"""
    rows = table.get("rows", [])
    if not rows:
        return 0
    signature = _get_table_body_signature(rows)
    typical = signature.get("typical_col_count", 0)
    if typical >= 2:
        return typical
    return max((len(r.get("items", [])) for r in rows), default=0)


def _find_missing_headers_in_previous(
    tables: List[dict],
    current_idx: int,
    data_cols: int,
    need_year: bool,
    need_entity: bool,
) -> List[List[dict]]:
    """在之前的表格中搜索缺失的主表头 items。

    搜索优先级：
    1. 同页前一表的 _stripped_tail_row_items
    2. 跨页：向前一页最后若干个表回溯

    返回：找到的主表头 items 列表（按从近到远排列），或空列表。
    """
    if not need_year and not need_entity:
        return []

    current_page = tables[current_idx].get("page", -1)

    # 收集候选源表
    candidates = []

    # 同页前一表
    if current_idx > 0:
        prev = tables[current_idx - 1]
        if prev.get("page", -1) == current_page:
            candidates.append(prev)

    # 跨页：前一页的表
    if not candidates:
        for prev_idx in range(current_idx - 1, max(current_idx - 6, -1), -1):
            pt = tables[prev_idx]
            if pt.get("page", -1) < current_page:
                candidates.append(pt)
                # 收集前一页的所有表（可能有多个，如 P14-15-16 合并）
                for pp_idx in range(prev_idx - 1, max(prev_idx - 4, -1), -1):
                    if tables[pp_idx].get("page", -1) == pt.get("page", -1):
                        candidates.append(tables[pp_idx])
                    else:
                        break
                break

    if not candidates:
        return []

    # 在候选表中搜索
    found = []
    for src_table in candidates:
        stripped_items_list = src_table.get("_stripped_tail_row_items", [])
        if not stripped_items_list:
            continue

        # 从尾部往前（最近的先匹配）
        for sitems in reversed(stripped_items_list):
            if not sitems or sitems in found:
                continue

            texts = [it.get("text", "") for it in sitems]

            # 检查是否匹配缺失的类型
            matches = False
            if need_year and _row_has_year_header(texts, data_cols):
                matches = True
            elif need_entity and _row_has_entity_header(texts, data_cols):
                matches = True

            if matches:
                found.append(sitems)
                # 清理源表记录
                _remove_header_from_source(src_table, sitems)

                # 如果两种类型都找到了 → 停止搜索
                found_year = any(
                    _row_has_year_header(
                        [it.get("text", "") for it in fi], data_cols
                    )
                    for fi in found
                )
                found_entity = any(
                    _row_has_entity_header(
                        [it.get("text", "") for it in fi], data_cols
                    )
                    for fi in found
                )
                if (not need_year or found_year) and (not need_entity or found_entity):
                    return found

    return found


def _row_has_year_header(texts: List[str], data_cols: int) -> bool:
    """检查一行是否含年份表头特征（≥2年份 + 列对齐）。"""
    non_empty = [t.strip() for t in texts if t.strip()]
    year_count = sum(1 for t in non_empty if YEAR_PATTERN.search(t))
    if year_count < 2:
        return False
    # 排除长数值
    if any(re.search(r'\d[\d,.]{4,}', t) for t in non_empty):
        return False
    # 列对齐（用 non_empty 数）
    return abs(len(non_empty) - data_cols) <= 3


def _row_has_entity_header(texts: List[str], data_cols: int) -> bool:
    """检查一行是否含实体表头特征（实体标签 + 列对齐）。

    实体标签如 "本集团"、"本行" 通常占据部分列，
    所以 items/非空数会少于数据列数。容忍度放宽到 ±5。
    """
    non_empty = [t.strip() for t in texts if t.strip()]
    if not any(t in ENTITY_KW for t in non_empty):
        return False
    # 实体标签行 items 数可能只有数据列的一半 → 容忍度 ±5
    return abs(len(non_empty) - data_cols) <= 5


def _remove_header_from_source(src_table: dict, recovered_items: List[dict]):
    """从源表的 stripped 记录中移除已恢复的 items。"""
    # 构造匹配文本
    recovered_text = " ".join(
        it.get("text", "").strip() for it in recovered_items
        if it.get("text", "").strip()
    )
    norm_recovered = re.sub(r'\s+', '', recovered_text)

    # 清理 _stripped_tail_rows
    stripped_texts = src_table.get("_stripped_tail_rows", [])
    if stripped_texts:
        src_table["_stripped_tail_rows"] = [
            t for t in stripped_texts
            if re.sub(r'\s+', '', t) != norm_recovered
        ]

    # 清理 _stripped_tail_row_items（用 identity 比较）
    stripped_items = src_table.get("_stripped_tail_row_items", [])
    if stripped_items:
        src_table["_stripped_tail_row_items"] = [
            sitems for sitems in stripped_items
            if sitems is not recovered_items
        ]


# --- 规则3：表前文本行清理 ---

def _find_first_table_header_or_data_row(rows: List[dict]) -> int:
    """找到表格中第一个表头行或数据行的索引。

    从上往下扫描，用两层判断：
    1. 表头行：多列 + 年份/变化/结构关键词、或全短标签
    2. 列签名匹配：若该行的列数和数据类型匹配表格主体签名 → 属于表格

    单位行、单列长文本 → 继续往下跳过。

    Returns:
        第一个表头行或数据行的索引，若全是文本行则返回 0
    """
    if len(rows) < 2:
        return 0

    # 先获取表格整体签名，辅助判断"是否属于表格数据"
    signature = _get_table_body_signature(rows)
    has_valid_sig = signature["typical_col_count"] >= 2

    for ri, row in enumerate(rows):
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue

        items_in_row = row.get("items", [])
        n_items = len(items_in_row)
        n_texts = len(non_empty)

        # ---- 检测表头行 ----
        is_header = False

        # 信号A：多列 + 年份关键词
        has_year = any(YEAR_PATTERN.search(t) for t in non_empty)
        if has_year and n_texts >= 2:
            is_header = True

        # 信号B：多列 + 变化/增减关键词
        has_delta = any(any(kw in t for kw in DELTA_KW) for t in non_empty)
        if has_delta and n_texts >= 2:
            is_header = True

        # 信号C：多列 + 表格结构关键词（使用统一常量）
        has_header_kw = any(any(kw in t for kw in HEADER_KW) for t in non_empty)
        if has_header_kw and n_texts >= 2:
            is_header = True

        # 信号D：全短标签（每列 ≤10字符）+ 多列（≥3列）+ 零数值单元格
        #      从6放宽到10以容纳更多中文表头，加零数值守卫避免误判数据行
        all_short = all(len(t) <= 10 for t in non_empty)
        zero_numeric = all(not _is_cell_numeric_like(t) for t in non_empty)
        if all_short and zero_numeric and n_texts >= 3:
            is_header = True

        # 信号E：双实体表头（"本集团"+"本行"、"合并"+"母公司"等同时出现）
        if _is_dual_entity_header_row(non_empty):
            is_header = True

        if is_header:
            return ri

        # ---- 列签名匹配：列数+数据类型符合表格主体 → 属于表格 ----
        if has_valid_sig:
            is_match, _ = _row_matches_body_signature(row, signature)
            if is_match:
                return ri

        # ---- 单位行 → 跳过 ----
        if _is_unit_info_row(non_empty):
            continue

        # ---- 纯文本行（少列 + 长文本 + 无数据）→ 跳过 ----
        if n_texts <= 2:
            total_len = sum(len(t) for t in non_empty)
            if total_len > 20:
                continue

    return 0  # 没找到明确的表头/数据行，保持原样


def _is_unit_info_row(non_empty_texts: List[str]) -> bool:
    """判断一行是否为单位信息行（如"单位：万元""Unit: million"）。

    新增：识别括号括起来的单位说明，如"（人民币百万元，百分比除外）"
    """
    all_text = "".join(non_empty_texts)
    unit_patterns = [
        r'单位\s*[：:]\s*\S',
        r'Unit\s*:\s*\S',
        r'单位[：:]',
        r'Unit:',
    ]
    for pat in unit_patterns:
        if re.search(pat, all_text, re.IGNORECASE):
            return True

    # 括号括起来的单位说明（如"（人民币百万元，百分比除外）"）
    bracketed = re.search(r'[（(]\s*[^）)]*[）)]', all_text)
    if bracketed and bracketed.group() == all_text.strip():
        # 整个文本就是括号内容 → 可能是单位/注释说明
        inner = bracketed.group()[1:-1]  # 去掉括号
        # 包含单位词或"除外"等注释标记
        has_unit = any(kw in inner for kw in UNIT_KW)
        has_note = any(kw in inner for kw in ['除外', '列示', '注明', '披露'])
        if has_unit or has_note:
            return True

    # 也检查是否仅含"万元""千元""百万元"等纯单位词
    for kw in UNIT_KW:
        if all_text.strip() == kw:
            return True
    return False


def _extract_unit_info_from_row(non_empty_texts: List[str]) -> str:
    """从单位信息行提取单位文本（如"单位：万元"→"万元"）。"""
    all_text = "".join(non_empty_texts)
    # 尝试匹配 "单位：XXX" 或 "单位: XXX"
    m = re.search(r'单位\s*[：:]\s*(.+)', all_text)
    if m:
        return m.group(1).strip()
    m = re.search(r'Unit\s*:\s*(.+)', all_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 纯单位词（使用统一常量）
    for kw in UNIT_KW:
        if kw in all_text:
            return kw
    return all_text.strip()


def _strip_leading_text_rows(
    table: dict, rows: List[dict], header_start: int
) -> bool:
    """移除表头行之前的多余文本行，单位信息补充到 caption。

    Args:
        table: 表格字典（会被原地修改）
        rows: 当前行列表
        header_start: 第一个表头/数据行的索引

    Returns:
        True 如果有行被移除
    """
    if header_start <= 0 or header_start >= len(rows):
        return False

    removed_rows = rows[:header_start]
    kept_rows = rows[header_start:]

    if len(kept_rows) < 2:
        return False  # 保留的行太少，不操作

    # 提取单位信息
    unit_info = ""
    for removed_row in removed_rows:
        texts = removed_row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]
        if not non_empty:
            continue
        if _is_unit_info_row(non_empty):
            unit_info = _extract_unit_info_from_row(non_empty)
            break  # 只取第一个单位信息

    # 更新 caption
    if unit_info:
        old_caption = table.get("caption", "").strip()
        if unit_info not in old_caption:
            if old_caption:
                table["caption"] = f"{old_caption}（{unit_info}）"
            else:
                table["caption"] = f"单位：{unit_info}"

    # 记录移除详情
    removed_texts = []
    for r in removed_rows:
        t = " ".join(t.strip() for t in r.get("texts", []) if t.strip())
        if t:
            removed_texts.append(t)
    table["_stripped_leading_rows"] = removed_texts

    # 重建表格
    _rebuild_table_from_rows(table, kept_rows)
    return True


# --- 列结构签名（核心：判断一行是否属于表格） ---

def _get_table_body_signature(rows: List[dict]) -> dict:
    """从表格主体行提取列结构签名。

    动态检测表头结束位置（非硬编码 skip 2），仅用"含数值的数据行"计算签名：
    - typical_col_count: 最常见的 items 数（取 ≥2 的众数，优先用 raw items 数）
    - col_numeric_ratio: 每列中数值型单元格的比例（0.0=纯文本列, 1.0=纯数值列）

    这是判断任意一行"是否属于此表格"的基准。
    """
    if not rows:
        return {"typical_col_count": 0, "col_numeric_ratio": []}

    # —— 动态检测表头结束位置 ——
    # 连续 ≥2 行含 ≥2 个数值单元格 → 表头结束，数据开始
    data_start = 0
    consecutive_numeric = 0
    for ri, row in enumerate(rows):
        texts = row.get("texts", [])
        numeric_count = sum(1 for t in texts if _is_cell_numeric_like(t))
        if numeric_count >= 2:
            consecutive_numeric += 1
            if consecutive_numeric >= 2:
                data_start = ri - 1  # 回退一行（第一行含数值的可能是混合行）
                data_start = max(0, data_start)
                break
        else:
            consecutive_numeric = 0

    # 如果没找到连续数据行，用原来的2行兜底
    if data_start == 0 and len(rows) > 2:
        data_start = min(2, len(rows) - 1)

    data_rows = rows[data_start:]
    if not data_rows:
        data_rows = rows

    # —— 仅保留含数值的数据行来计算签名（过滤脚注等非数据行） ——
    numeric_data_rows = [
        r for r in data_rows
        if sum(1 for t in r.get("texts", []) if _is_cell_numeric_like(t)) >= 1
    ]
    if not numeric_data_rows:
        # 无含数值的行 → 降级使用全部 data_rows
        numeric_data_rows = data_rows

    # 统计最常见 items 数（取 ≥2 的众数，用 raw items 避免被补齐列数干扰）
    from collections import Counter
    item_counts = Counter(len(r.get("items", [])) for r in numeric_data_rows)
    typical_col_count = 0
    for cc in sorted(item_counts.keys(), reverse=True):
        if cc >= 2:
            typical_col_count = cc
            break
    if typical_col_count == 0 and item_counts:
        typical_col_count = max(item_counts.keys())

    if typical_col_count <= 1:
        return {"typical_col_count": 0, "col_numeric_ratio": []}

    # 逐列统计数值型比例（用 texts 对齐到 typical_col_count）
    col_numeric_sum = [0.0] * typical_col_count
    col_non_empty_count = [0] * typical_col_count

    for row in numeric_data_rows:
        texts = row.get("texts", [])
        for ci in range(min(typical_col_count, len(texts))):
            t = texts[ci].strip() if ci < len(texts) else ""
            if t:
                col_non_empty_count[ci] += 1
                if _is_cell_numeric_like(t):
                    col_numeric_sum[ci] += 1

    col_numeric_ratio = []
    for ci in range(typical_col_count):
        if col_non_empty_count[ci] > 0:
            col_numeric_ratio.append(col_numeric_sum[ci] / col_non_empty_count[ci])
        else:
            col_numeric_ratio.append(0.0)

    return {
        "typical_col_count": typical_col_count,
        "col_numeric_ratio": col_numeric_ratio,
    }


def _is_cell_numeric_like(text: str) -> bool:
    """判断单元格是否像数值型（允许夹带少量中文）。

    例如 "1.34" → True, "1,234万元" → True, "增长12.5%" → True
    但 "净利润除以..." → False, "第9号" → False（中文太多）
    日期格式 "18/11/2014" "12月31日" → False
    纯年号 "2024年" "2023年" → False（表头年份列，不是数据）
    """
    t = text.strip()
    if not t:
        return False
    # 排除日期格式（防止日期列的数值误判）
    if DATE_PATTERN.search(t):
        return False
    # 排除纯年号（如 "2024年""2023年"）—— 表头年份列，不是数据
    if YEAR_ONLY.match(t):
        return False
    # 排除划线占位符（–、—、- 等表示"无数据/不适用"）
    if DASH_ONLY.match(t):
        return False
    # 排除脚注编号标记（"1." "2." "(1)" "1）" 等 — 有括号或后缀的纯编号）
    # 注意：不过度匹配纯数字（如"599"），必须含括号或句点等编号标记
    if re.match(r'^[\s　]*[\(（]\d{1,3}[\)）]?[\s　]*$', t):
        return False
    if re.match(r'^[\s　]*\d{1,3}[\.、][\s　]*$', t):
        return False
    # 纯数值
    if _is_numeric_cell(t):
        return True
    # 包含数字 → 看中文比例
    if re.search(r'\d', t):
        digits = len(re.findall(r'\d', t))
        chinese = len(re.findall(r'[\u4e00-\u9fff]', t))
        # 中文过多（中文数 > 数字数）→ 不是数值型
        # 如"第9号"(2中文>1数字) → False, "1,234万元"(2中文<4数字) → True
        if chinese > digits:
            return False
        return True
    return False


def _row_matches_body_signature(
    row: dict, signature: dict
) -> Tuple[bool, float]:
    """判断一行是否匹配表格主体的列结构签名。

    两个维度：
    1. 原始 items 数是否接近 typical_col_count（差异 ≤3 以内）
       **关键**：用 raw items 数而非补齐后的 texts 数，避免归一化让所有行
       列数一致（脚注行1个item被补齐到7列后无法区分）。
    2. 各列数据类型是否匹配（该是数值的列是数值型，该是文本的列是文本型）
       - 数值列可以容忍少量中文（如"1,234万元"）
       - 汇总行关键词（合计/小计）在数值列也接受

    Returns:
        (is_match, score) — score 0.0~1.0, ≥0.55 视为匹配
    """
    typical = signature.get("typical_col_count", 0)
    col_ratios = signature.get("col_numeric_ratio", [])

    if typical <= 1 or not col_ratios:
        return False, 0.0

    texts = row.get("texts", [])
    # 用原始 items 数（非补齐后的 texts 数）做列数比较
    # 否则归一化后所有行列数相同，脚注行也能拿满分
    n_items = len(row.get("items", []))

    # ---- 维度1：列数匹配 ----
    col_diff = abs(n_items - typical)
    if col_diff > 3:
        return False, 0.0
    if col_diff == 0:
        col_score = 1.0
    elif col_diff == 1:
        col_score = 0.8
    elif col_diff == 2:
        col_score = 0.5
    else:
        col_score = 0.2

    # ---- 维度2：逐列数据类型匹配 ----
    n_texts = len(texts)
    if n_texts == 0:
        return False, 0.0

    type_matches = 0.0
    total_compared = 0

    for ci in range(min(typical, n_texts)):
        t = texts[ci].strip() if ci < len(texts) else ""
        col_is_numeric_col = col_ratios[ci] >= 0.5  # 该列通常是数值列

        if not t:
            continue  # 空单元格不扣分

        total_compared += 1
        cell_is_numeric = _is_cell_numeric_like(t)

        if cell_is_numeric == col_is_numeric_col:
            type_matches += 1.0
        elif col_is_numeric_col and not cell_is_numeric:
            # 数值列出现纯文本 → 扣分（但汇总行关键词容忍）
            if any(kw in t for kw in SUMMARY_KW):
                type_matches += 1.0
            else:
                type_matches += 0.0
        else:
            # 文本列出现数值 → 一半容忍
            type_matches += 0.5

    if total_compared == 0:
        type_score = 0.5
    else:
        type_score = type_matches / total_compared

    # 综合：列数权重 0.3，类型匹配权重 0.7
    score = col_score * 0.3 + type_score * 0.7

    # 空格惩罚：当大多数列（≥4列的表中仅≤1列有内容）为空时降权
    # 典型受益者：
    #   "率和平均成本率变动带动利息净收入减少717.06亿元。" → 1有效 vs 4典型 → 降权
    # 不影响的场景：
    #   "发放贷款和垫款总额 81,076 (94,091) (13,015)" → 4有效 vs 4典型 → 正常分值
    if typical >= 4 and total_compared <= 1:
        score *= 0.5

    return score >= 0.55, score


# --- 规则1a：尾部脚注/注释行清理 ---


def _is_annotation_marker(text: str) -> bool:
    """判断一个单元格文本是否以脚注/注释编号标记开头。

    支持多种标记格式：
    - 数字+标点: "1.", "2.", "3、"
    - 括号编号: "(1)", "1）", "1)"
    - 圆圈数字: "①②③④"
    - 中文编号: "（一）", "（二）"
    - 符号标记: "*", "**", "†", "‡"
    - 方括号: "[1]"
    """
    stripped = text.strip()
    if not stripped:
        return False

    patterns = [
        r'^\d{1,2}[\.\、．)\）]\s*',          # 1. 或 1） 或 1)
        r'^\(\d{1,2}\)\s*',                   # (1)
        r'^[（\(]\d{1,2}[）\)]\s*',           # （1）
        r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*',   # ①
        r'^[（\(][一二三四五六七八九十]+[）\)]\s*', # （一）
        r'^[\*†‡]{1,3}[\s　]*',               # * 或 ** 或 †
        r'^\[\d{1,2}\]\s*',                   # [1]
    ]
    for pat in patterns:
        if re.search(pat, stripped):
            return True
    return False


def _count_chinese_chars(text: str) -> int:
    """统计文本中中文字符的数量。"""
    return len(re.findall(r'[\u4e00-\u9fff]', text))


def _is_annotation_row(row: dict) -> bool:
    """判断一行是否为脚注/注释行。

    从多个维度综合判断：
    1. 首列是编号标记 + 无数据数值 + 中文长文本
    2. 以注释关键词开头（注：、附注：、资料来源：等）
    3. 长文本（≥50字符）+ 无数值 + 原始 item ≤ 2
    4. 1-2个单元格的纯中文解释句（以句号结尾、无数值）
    """
    texts = row.get("texts", [])
    items = row.get("items", [])
    non_empty = [t.strip() for t in texts if t.strip()]

    if not non_empty:
        return False

    all_text = "".join(non_empty)
    first_cell = non_empty[0]
    total_len = sum(len(t) for t in non_empty)
    chinese_count = _count_chinese_chars(all_text)

    def _strip_annotation_prefix(text: str) -> str:
        """去掉脚注编号前缀，如 '1.  内容' -> '内容'"""
        for pat in [
            r'^\d{1,2}[\.\、．)\）]\s*',
            r'^\(\d{1,2}\)\s*',
            r'^[（\(]\d{1,2}[）\)]\s*',
            r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*',
            r'^[（\(][一二三四五六七八九十]+[）\)]\s*',
            r'^[\*†‡]{1,3}[\s　]*',
            r'^\[\d{1,2}\]\s*',
        ]:
            m = re.match(pat, text)
            if m:
                return text[m.end():]
        return text

    # 统计非年份的数值 token 数量（排除脚注编号前缀中的数字）
    numeric_count = 0
    has_annotation_prefix = _is_annotation_marker(first_cell)
    for i, t in enumerate(non_empty):
        # 对首列且带注释前缀的文本，先去掉前缀再统计数字
        if i == 0 and has_annotation_prefix:
            t_clean = _strip_annotation_prefix(t)
        else:
            t_clean = t

        numbers = re.findall(r'-?\d+\.?\d*', t_clean)
        for n in numbers:
            try:
                val = float(n)
            except ValueError:
                continue
            # 排除纯年份（1900-2100）和纯页码数字
            if 1900 <= val <= 2100:
                continue
            # 排除长文本中常见的报告编号（如"第9号"中的9）
            if val <= 20:
                # 检查前后文是否为报告/文件/规则编号
                pos_in_clean = t_clean.find(n)
                ctx_before = t_clean[max(0, pos_in_clean - 5):pos_in_clean]
                ctx_after = t_clean[pos_in_clean + len(n):pos_in_clean + len(n) + 3]
                if re.search(r'(第|号|规则|编报|披露|公告|CIRCULAR|Report|No\.)', ctx_before + ctx_after):
                    continue
            numeric_count += 1

    # ---- 条件1：首列为编号标记 + 无数据数值 + 中文长文本 ----
    if has_annotation_prefix:
        if numeric_count == 0 and chinese_count >= 5:
            if total_len >= 15:
                return True
            # 短注释但以中文句号结尾（完整注释句），如 "1.\t包括债权类投资。"
            if all_text.rstrip().endswith('。') and total_len >= 8:
                return True

    # ---- 条件2：以注释关键词开头 ----
    annotation_keywords = [
        '注：', '注释：', '备注：', '说明：', '附注：',
        '资料来源：', '数据来源：', 'Source:', 'Note:', 'Notes:',
    ]
    for kw in annotation_keywords:
        if first_cell.startswith(kw):
            if numeric_count <= 1 and chinese_count >= 5:
                return True

    # ---- 条件3：极长文本（≥50字符）+ 无数值 + item 少 ----
    if total_len >= 50 and numeric_count == 0 and len(items) <= 2 and chinese_count >= 15:
        return True

    # ---- 条件4：纯中文解释句（以句号结尾、无数值、1-2个单元格） ----
    if len(non_empty) <= 2 and total_len >= 20 and chinese_count >= 8:
        if all_text.rstrip().endswith(('。', '.', '）', ')')):
            if numeric_count == 0:
                return True

    # ---- 条件5：结构差异 + 中文长文本注释续行 ----
    # 针对注释段落的后续行（不是编号开头的那行），特征：
    #   - 结构窄（原始 items ≤ 2），与多列表格主体结构不同
    #   - 包含大量中文（≥15字），是注释段落而非表格数据
    #   - 嵌入数字极少（≤2），即使有也是"717.06亿元"这类说明性数字
    # 三重条件缺一不可，确保不误删：
    #   "发放贷款和垫款总额 81,076" → chinese=8 < 15 → 不触发 ✓
    #   "资产" → chinese=2 < 15 → 不触发 ✓
    #   "利息收入变化 104,354 (110,163)" → items=4 > 2 → 不触发 ✓
    if len(items) <= 2 and chinese_count >= 15 and numeric_count <= 2:
        return True

    return False


def _strip_tail_annotation_rows(table: dict, rows: List[dict]) -> bool:
    """从表格底部移除脚注/注释行。

    与 _strip_tail_non_data_rows 的区别：
    - 专门针对编号脚注行（如"1. 净利润除以..."）
    - 触发阈值：≥1 行脚注即可移除
    - 遇到汇总行或数据主体行 → 停止扫描
    - 至少保留 3 行安全底线

    返回：是否有行被移除
    """
    if len(rows) < 4:
        return False

    # 从底部向上扫描，统计连续的脚注行
    annotation_count = 0
    for ri in range(len(rows) - 1, -1, -1):
        row = rows[ri]
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]

        if not non_empty:
            annotation_count += 1
            continue

        all_text = "".join(non_empty)

        # 先检查是否为注释行（优先级高于汇总关键词，避免"平均值"等干扰）
        if _is_annotation_row(row):
            annotation_count += 1
            continue

        # 汇总行 → 保留，停止
        if any(kw in all_text for kw in SUMMARY_KW):
            break

        # 文本换行续行检测：极短文本（≤20字符）+ 无数字 → 视为注释文本换行延续
        # 典型场景：PDF 提取中长句被误拆为多行，如 "不含应计利" + "息。"
        if len(all_text) <= 20 and not any(c.isdigit() for c in all_text):
            annotation_count += 1
            continue

        # 遇到非脚注、非汇总、非续行的行 → 停止（说明已经是表格主体数据行）
        break

    if annotation_count < 1:
        return False

    # 安全底线：至少保留 3 行
    actual_remove = annotation_count
    if len(rows) - actual_remove < 3:
        actual_remove = max(0, len(rows) - 3)

    if actual_remove <= 0:
        return False

    # 记录移除详情
    removed_rows = rows[-actual_remove:]
    removed_texts = []
    for r in removed_rows:
        t = " ".join(t.strip() for t in r.get("texts", []) if t.strip())
        if t:
            removed_texts.append(t)
    table["_stripped_annotation_rows"] = removed_texts

    # 同时追加到 _stripped_tail_rows（与现有逻辑兼容）
    if "_stripped_tail_rows" not in table:
        table["_stripped_tail_rows"] = []
    table["_stripped_tail_rows"].extend(removed_texts)

    kept_rows = rows[:-actual_remove]
    _rebuild_table_from_rows(table, kept_rows)
    return True


# --- 规则1b：底部尾巴行清理 ---

def _strip_tail_non_data_rows(table: dict, rows: List[dict]) -> bool:
    """从表格底部移除不属于表格的行。

    策略：
    1. 从表格主体行提取列结构签名（列数 + 每列数值/文本类型）
    2. 从底部向上扫描，非首列无数据数值的行直接判定为非数据行
    3. 汇总行（"合计""总计"等）→ 保留并停止
    4. 底部存在非数据行即触发移除
    5. 至少保留 3 行
    """
    if len(rows) < 4:
        return False

    signature = _get_table_body_signature(rows)
    if signature["typical_col_count"] <= 1:
        return False

    # 从底部向上扫描
    consecutive_non_match = 0

    for ri in range(len(rows) - 1, -1, -1):
        row = rows[ri]
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]

        if not non_empty:
            consecutive_non_match += 1
            continue

        all_text = "".join(non_empty)

        # 汇总行 → 保留，停止
        if any(kw in all_text for kw in SUMMARY_KW):
            break

        # 非首列无数据数值 → 肯定不是数据行，直接计入
        non_first = non_empty[1:] if len(non_empty) > 1 else []
        if not any(_is_cell_numeric_like(t) for t in non_first):
            consecutive_non_match += 1
            continue

        # 用签名匹配判断
        is_match, _ = _row_matches_body_signature(row, signature)
        if is_match:
            break
        else:
            consecutive_non_match += 1

    if consecutive_non_match < 1:
        return False

    # 计算实际要移除的行数
    actual_remove = 0
    for ri in range(len(rows) - 1, -1, -1):
        row = rows[ri]
        texts = row.get("texts", [])
        non_empty = [t.strip() for t in texts if t.strip()]

        if not non_empty:
            actual_remove += 1
            continue

        all_text = "".join(non_empty)
        if any(kw in all_text for kw in SUMMARY_KW):
            break

        # 非首列无数据数值 → 移除
        non_first = non_empty[1:] if len(non_empty) > 1 else []
        if not any(_is_cell_numeric_like(t) for t in non_first):
            actual_remove += 1
            continue

        is_match, _ = _row_matches_body_signature(row, signature)
        if is_match:
            break

        actual_remove += 1

    if actual_remove < 1:
        return False

    # 安全底线：至少保留 3 行
    if len(rows) - actual_remove < 3:
        actual_remove = max(0, len(rows) - 3)

    if actual_remove <= 0:
        return False

    # 记录移除详情
    removed_rows = rows[-actual_remove:]
    removed_texts = []
    for r in removed_rows:
        t = " ".join(t.strip() for t in r.get("texts", []) if t.strip())
        if t:
            removed_texts.append(t)
    table["_stripped_tail_rows"] = removed_texts
    # 同时保存原始 items，用于后续表头恢复检测
    table["_stripped_tail_row_items"] = [list(r.get("items", [])) for r in removed_rows]

    kept_rows = rows[:-actual_remove]
    _rebuild_table_from_rows(table, kept_rows)
    return True


# --- 规则2：跨页续表检测 ---

def _mark_cross_page_candidate_if_match(
    tables: List[dict], idx_a: int, idx_b: int
):
    """检查 table_b（缺表头）是否为 table_a 的跨页续表候选。

    判断条件：
    1. 列数相近（差异 ≤2）
    2. 在同一页或相邻页

    若满足 → 给 table_b 标记 _cross_page_candidate = True
    """
    table_a = tables[idx_a]
    table_b = tables[idx_b]

    # 页码检查：同页或相邻页
    pg_a = table_a.get("page", 0)
    pg_b = table_b.get("page", 0)
    if pg_b not in (pg_a, pg_a + 1):
        return

    # 列数对比
    cols_a = len(table_a.get("column_x_ranges", []))
    cols_b = len(table_b.get("column_x_ranges", []))

    if cols_a == 0 or cols_b == 0:
        rows_a = table_a.get("rows", [])
        rows_b2 = table_b.get("rows", [])
        if rows_a:
            cols_a = max(len(r.get("texts", [])) for r in rows_a)
        if rows_b2:
            cols_b = max(len(r.get("texts", [])) for r in rows_b2)

    if cols_a <= 0 or cols_b <= 0:
        return
    if abs(cols_a - cols_b) > 2:
        return

    table_b["_cross_page_candidate"] = True
    table_b["_cross_page_ref_table_id"] = table_a.get("table_id", -1)


# --- 重建与重新分类工具 ---

def _rebuild_table_from_rows(table: dict, new_rows: List[dict]):
    """用新的 rows 列表原地重建表格的 text_items、坐标等信息。"""
    all_items = []
    for r in new_rows:
        all_items.extend(r.get("items", []))

    if not all_items:
        return

    # 按 Y, X 排序
    all_items.sort(key=lambda it: (it.get("y_mid", 0), it.get("x0", 0)))

    col_ranges = _estimate_column_x_ranges(new_rows)
    col_schema = infer_column_schema(new_rows)

    y0 = min(it.get("y0", float("inf")) for it in all_items)
    y1 = max(it.get("y1", 0) for it in all_items)

    table["rows"] = new_rows
    table["text_items"] = all_items
    table["y0"] = y0
    table["y1"] = y1
    table["row_count"] = len(new_rows)
    table["column_x_ranges"] = col_ranges
    table["column_schema"] = col_schema


def _reclassify_single_table(table: dict):
    """在行变动后重新评估单张表格的分类。

    仅更新 table_category, is_real_table, is_complete, has_header, has_numeric_data
    等分类字段，不覆盖原有的 quality_reason 等。
    """
    rows = table.get("rows", [])
    if not rows:
        return

    row_count = len(rows)
    max_cols = max((len(r.get("texts", [])) for r in rows), default=0)

    # 检测表头（动态深度：扫描前 1/4 行或最多 6 行）
    has_header = False
    header_scan_limit = max(2, min(6, len(rows) // 4))
    for ri in range(min(header_scan_limit, len(rows))):
        texts = rows[ri].get("texts", [])
        if _is_header_like(texts):
            has_header = True
            break
        all_text = "".join(texts)
        if any(kw in all_text for kw in HEADER_KW) and len(texts) >= 2:
            has_header = True
            break

    # 动态计算实际表头行数
    if has_header:
        header_skip = 1
        for ri in range(1, min(header_scan_limit, len(rows))):
            texts = rows[ri].get("texts", [])
            if _is_header_like(texts) or (
                any(kw in "".join(texts) for kw in HEADER_KW) and len(texts) >= 2
            ):
                header_skip += 1
            else:
                break
    else:
        header_skip = 1

    # 检测数值数据
    has_numeric_data = False
    numeric_col_count = 0
    if max_cols >= 2:
        for c in range(max_cols):
            col_vals = []
            for ri, row in enumerate(rows):
                texts = row.get("texts", [])
                if c < len(texts) and ri >= header_skip:
                    val = texts[c].strip()
                    if val:
                        col_vals.append(val)
            if not col_vals:
                continue
            numeric_in_col = sum(1 for v in col_vals if _is_numeric_cell(v))
            ratio = numeric_in_col / max(len(col_vals), 1)
            if ratio >= 0.5 and numeric_in_col >= 2:
                numeric_col_count += 1
                has_numeric_data = True

    # 判定
    is_real_table = has_numeric_data and max_cols >= 2 and row_count >= 3
    is_complete = has_header and row_count >= 3

    if not is_real_table:
        if numeric_col_count == 0:
            # 回退检查：has_header 且 header_skip > 1 时，数据行可能被过度跳过
            if has_header and header_skip > 1 and max_cols >= 2:
                fb_numeric_col_count = 0
                for c in range(max_cols):
                    col_vals_fb = []
                    for ri, row in enumerate(rows):
                        texts = row.get("texts", [])
                        if c < len(texts) and ri >= 1:
                            val = texts[c].strip()
                            if val:
                                col_vals_fb.append(val)
                    if not col_vals_fb:
                        continue
                    n_fb = sum(1 for v in col_vals_fb if _is_numeric_cell(v))
                    if n_fb >= 1 and n_fb / len(col_vals_fb) >= 0.3:
                        fb_numeric_col_count += 1
                if fb_numeric_col_count > 0:
                    category = "非标准表格"
                    has_numeric_data = True
                    numeric_col_count = fb_numeric_col_count
                    is_real_table = True
                else:
                    category = "文本列表"
            else:
                category = "文本列表"
        else:
            category = "非标准表格"
    elif is_complete:
        category = "财务数据表"
    else:
        category = "数据表(缺表头)"

    table["is_real_table"] = is_real_table
    table["is_complete"] = is_complete
    table["has_header"] = has_header
    table["has_numeric_data"] = has_numeric_data
    table["table_category"] = category
    table["numeric_col_count"] = numeric_col_count

    # 更新 quality_checks
    table["quality_checks"] = {
        "has_header": has_header,
        "has_numeric_data": has_numeric_data,
        "numeric_col_count": numeric_col_count,
        "row_count": row_count,
        "col_count": max_cols,
    }


# ---- (7) 财务表格置信度评分 ----

def _compute_financial_confidence(tables: List[dict]) -> List[dict]:
    """基于 Phase 4 已有的各维度指标，为每张表计算财务表格置信度（0~1）。

    评分因子（对 is_real_table=True 的表）：
      - 数值列占比    （0 ~ 0.20）：numeric_col_count / col_count
      - 表头加成      （0 ~ 0.12）：has_header = True 时 +0.12
      - 行数丰富度    （0 ~ 0.08）：(row_count - 3) / 12，3 行 → 0，15+ 行 → +0.08
      - 基础分        （0.55）   ：通过 is_real_table 门槛

    对 is_real_table=False 的表，按 category 给底分，上限 0.45：
      - 空表        → 0.00
      - 目录        → 0.03
      - 文本列表    → 0.08
      - 非标准表格  → 0.15 + 数值列因子（最多到 0.45）

    Returns:
        添加了 financial_confidence 字段的表格列表
    """
    for table in tables:
        category = table.get("table_category", "")
        is_real = table.get("is_real_table", False)

        if category == "空表":
            table["financial_confidence"] = 0.0
            continue

        rows = table.get("rows", [])
        row_count = len(rows)
        col_count = max((len(r.get("texts", [])) for r in rows), default=1)

        if is_real:
            # ── 真表格：基准 0.55 + 三大因子 ──
            confidence = 0.55

            # 因子1：数值列占比（最多 +0.20）
            nc = table.get("numeric_col_count", 0)
            if col_count > 0:
                confidence += min(nc / col_count, 1.0) * 0.20

            # 因子2：表头加成（+0.12 或 0）
            if table.get("has_header", False):
                confidence += 0.12

            # 因子3：行数丰富度（最多 +0.08）
            if row_count > 3:
                confidence += min((row_count - 3) / 12, 1.0) * 0.08

            confidence = min(confidence, 1.0)
        else:
            # ── 非真表格：按 category 给底分，上限 0.45 ──
            if category == "目录":
                confidence = 0.03
            elif category == "文本列表":
                confidence = 0.08
            elif category == "非标准表格":
                # 有少量数值数据但不够门槛，分数略高
                nc = table.get("numeric_col_count", 0)
                if col_count > 0:
                    confidence = 0.15 + min(nc / col_count, 1.0) * 0.25
                else:
                    confidence = 0.15
            else:
                # 兜底：按实际数值占比估算
                all_texts = []
                for r in rows:
                    all_texts.extend(
                        t.strip() for t in r.get("texts", []) if t.strip()
                    )
                if all_texts:
                    numeric = sum(1 for t in all_texts if _is_numeric_cell(t))
                    confidence = 0.05 + (numeric / len(all_texts)) * 0.25
                else:
                    confidence = 0.02

            confidence = min(confidence, 0.45)

        table["financial_confidence"] = round(confidence, 3)

    return tables


# ================================================================
# 7.5 质量决策函数（Phase 1 最小决策逻辑 — 止血）
# ================================================================

# 页眉页脚检测正则（通用模式，不再硬编码特定银行/年报文本）
HEADER_FOOTER_PATTERNS = [
    re.compile(r"(银行|公司|集团)股份有限公司$"),        # 公司/银行页眉
    re.compile(r"20\d{2}年(度|半年度|年度)?报告"),       # 年报/半年报标题
    re.compile(r"管理层讨论[与和]分析"),                 # 管理层讨论与分析
    re.compile(r"财务回顾|经营情况讨论与分析"),           # 常见章节标题
    re.compile(r"财务报表附注"),                          # 财报附注章节
    re.compile(r"（除特别注明外"),                        # 单位说明
    re.compile(r"^\d+$"),                                # 独立页码
]


def _is_row_page_header_footer(texts: list) -> bool:
    """判断一行的非空文本是否属于页眉页脚/章节标题。

    使用通用正则模式匹配，不硬编码特定公司名称。
    """
    non_empty = [t.strip() for t in texts if t.strip()]
    if not non_empty:
        return False
    combined = "".join(non_empty)
    for pat in HEADER_FOOTER_PATTERNS:
        if pat.search(combined):
            return True
    return False


def _header_footer_row_ratio(rows: list) -> float:
    """计算候选表中页眉页脚行的占比。"""
    if not rows:
        return 0.0
    hf_count = sum(1 for r in rows if _is_row_page_header_footer(r.get("texts", [])))
    return hf_count / len(rows)


def _is_paragraph_row(row: dict) -> bool:
    """判断一行是否为段落文本行（非表格数据行）。

    判定特征：
    - 单行文本总长 > 40 个中文字符
    - 非空单元格数 <= 2
    - 不具备列对齐数值结构
    """
    texts = row.get("texts", [])
    non_empty = [t.strip() for t in texts if t.strip()]
    if not non_empty:
        return False

    combined_len = sum(len(t) for t in non_empty)

    # 条件1: 文本总长 > 40 字符
    if combined_len <= 40:
        return False

    # 条件2: 非空单元格 ≤ 2
    if len(non_empty) > 2:
        return False

    # 条件3: 不含数值单元格
    has_numeric = any(_is_numeric_cell(t) for t in non_empty)
    if has_numeric:
        return False

    return True


def _paragraph_row_ratio(rows: list) -> float:
    """计算候选表中段落文本行的占比。"""
    if not rows:
        return 0.0
    para_count = sum(1 for r in rows if _is_paragraph_row(r))
    return para_count / len(rows)


def _is_chart_label_region(table: dict) -> bool:
    """判断候选表是否为图表标签区域（百分比 + 短标签模式）。

    增强检测：在已有 `_is_chart_like` 基础上，
    补充"第一列百分比 + 第二列短中文标签 + 无表头"模式。
    """
    rows = table.get("rows", [])
    if not rows or len(rows) < 2:
        return False

    has_header = table.get("has_header", False)
    if has_header:
        return False

    # 统计首列百分比和第二列中文标签
    percent_first_col = 0
    chinese_second_col = 0
    valid_rows = 0

    for row in rows:
        texts = row.get("texts", [])
        if len(texts) < 2:
            continue
        valid_rows += 1
        first = texts[0].strip() if len(texts) > 0 else ""
        second = texts[1].strip() if len(texts) > 1 else ""

        if first.endswith("%"):
            percent_first_col += 1
        if second and re.search(r'[\u4e00-\u9fff]', second) and len(second) <= 8:
            chinese_second_col += 1

    if valid_rows < 3:
        return False

    # 大部分行符合"百分比 + 短中文标签"模式
    if percent_first_col >= valid_rows * 0.5 and chinese_second_col >= valid_rows * 0.5:
        return True

    return False


def decide_table_acceptance(table: dict) -> dict:
    """返回表格最终去向和理由（Phase 1 最小决策逻辑）。

    仅依赖现有字段，不引入需要新实现的检测器。
    目标：止血 — 防止文本列表/图表标签污染主结果。

    Args:
        table: seg_table 字典，需含 table_category, financial_confidence,
               is_real_table, rows 等字段。

    Returns:
        {
            "decision": "accepted" | "review" | "rejected",
            "reason": "...",
            "score": 0.0,
            "flags": [...]
        }
    """
    category = table.get("table_category", "")
    conf = table.get("financial_confidence", 0.0)
    is_real = table.get("is_real_table", False)
    rows = table.get("rows", [])
    row_count = len(rows)
    col_count = max((len(r.get("texts", [])) for r in rows), default=0)

    flags = []
    score = 0.0

    # ── 硬拒绝 ──
    if not rows:
        return {"decision": "rejected", "reason": "空表", "score": 0.0, "flags": []}

    if category in ("文本列表", "目录", "空表"):
        return {"decision": "rejected", "reason": f"非财务表类型: {category}", "score": 0.0, "flags": []}

    if not is_real:
        return {"decision": "rejected", "reason": "缺少稳定数值列", "score": conf, "flags": ["no_real_table"]}

    # ── 页眉页脚检测 ──
    hf_ratio = _header_footer_row_ratio(rows)
    if hf_ratio > 0.3:
        return {"decision": "rejected", "reason": f"页眉页脚占比过高 ({hf_ratio:.0%})", "score": conf, "flags": ["page_header_footer_noise"]}
    if hf_ratio > 0:
        flags.append("page_header_footer_noise")

    # ── 段落文本检测 ──
    para_ratio = _paragraph_row_ratio(rows)
    if para_ratio > 0.4:
        return {"decision": "rejected", "reason": f"段落文本占比过高 ({para_ratio:.0%})", "score": conf, "flags": ["paragraph_noise"]}

    # ── 图表标签检测 ──
    if _is_chart_label_region(table):
        return {"decision": "rejected", "reason": "图表标签区域，不是结构化表格", "score": conf, "flags": ["chart_label"]}

    # ── 缺表头表 ──
    if category == "数据表(缺表头)":
        if conf >= 0.75 and row_count >= 6 and col_count >= 4:
            score = conf
            return {"decision": "review", "reason": "疑似续表或缺表头，需要人工确认", "score": score, "flags": flags + ["no_header"]}
        return {"decision": "rejected", "reason": "缺表头且结构弱", "score": conf, "flags": flags + ["no_header_weak"]}

    # ── 置信度分级 ──
    if category == "财务数据表" and conf >= 0.75:
        score = conf
        return {"decision": "accepted", "reason": "高可信财务数据表", "score": score, "flags": flags}

    if conf >= 0.55:
        score = conf
        return {"decision": "review", "reason": "中等置信度财务候选", "score": score, "flags": flags}

    return {"decision": "rejected", "reason": "置信度过低", "score": conf, "flags": flags}




# ================================================================
# 8. 格式转换：liteparse 表格 → 标准格式
# ================================================================

def _infer_column_x_ranges(rows: List[dict]) -> List[Tuple[float, float]]:
    """从 liteparse rows 推断统一的列 X 范围。

    每行的 col_x_ranges 可能因对齐方式略有偏差，取各列 x0 最小值
    和 x1 最大值作为该列的覆盖范围，确保不遗漏。

    Args:
        rows: liteparse 的 rows 列表，每个 row 含 col_x_ranges

    Returns:
        [(x0, x1), ...] 统一列范围列表
    """
    if not rows:
        return []

    # 取最大列数
    max_cols = max((len(r.get("col_x_ranges", [])) for r in rows), default=0)
    if max_cols == 0:
        return []

    unified = []
    for c in range(max_cols):
        x0_vals = []
        x1_vals = []
        for row in rows:
            ranges = row.get("col_x_ranges", [])
            if c < len(ranges) and ranges[c] is not None:
                x0, x1 = ranges[c]
                if x0 > 0 and x1 > 0:
                    x0_vals.append(x0)
                    x1_vals.append(x1)
        if x0_vals and x1_vals:
            unified.append((min(x0_vals), max(x1_vals)))

    return unified


def liteparse_tables_to_standard(
    seg_tables: List[dict],
    original_results: Optional[List[dict]] = None,
) -> List[dict]:
    """将 liteparse segmenter 输出的表格转为标准 pdf2docx 格式。

    Args:
        seg_tables: segment_tables_from_liteparse 的输出
        original_results: 原始的 pdf2docx/V2 提取结果（可选，用于保留元数据）

    Returns:
        标准格式表格列表，可直接用于 UI 显示和导出
    """
    import copy
    results = []

    for st in seg_tables:
        rows = st.get("rows", [])
        data = []
        for row in rows:
            texts = row.get("texts", [])
            data.append([str(t) if t is not None else "" for t in texts])

        # 从 rows 中推断统一的列 X 范围（用于 UI 渲染和调试）
        column_x_ranges = _infer_column_x_ranges(rows)

        page = st.get("page", 0)
        caption = st.get("caption", "")

        # 质量决策（Phase 1 最小逻辑）
        decision_info = decide_table_acceptance(st)
        decision = decision_info["decision"]
        reason = decision_info["reason"]
        score = decision_info["score"]
        flags = decision_info["flags"]

        # 根据决策设置 parse_status
        if decision == "accepted":
            parse_status = "success"
            parse_message = f"liteparse 分割 [可信: {reason}]"
        elif decision == "review":
            parse_status = "review"
            parse_message = f"liteparse 分割 [待复核: {reason}]"
        else:
            parse_status = "failed"
            parse_message = f"liteparse 分割 [已拒绝: {reason}]"

        result = {
            "page": page,
            "type": "table",
            "data": data,
            "extractor": "liteparse_segmenter",
            "title": caption or f"表格-P{page}",
            "parse_status": parse_status,
            "parse_message": parse_message,
            # 质量决策（Phase 1 新增 — 统一判定入口）
            "quality_decision": decision,
            "quality_decision_reason": reason,
            "quality_decision_score": score,
            "quality_flags": flags,
            # 质量标记
            "is_real_table": st.get("is_real_table", True),
            "is_complete": st.get("is_complete", True),
            "has_header": st.get("has_header", False),
            "has_numeric_data": st.get("has_numeric_data", False),
            "table_category": st.get("table_category", ""),
            "financial_confidence": st.get("financial_confidence", 0.0),
            "quality_reason": st.get("quality_reason", ""),
            "quality_checks": st.get("quality_checks", {}),
            "numeric_col_count": st.get("numeric_col_count", 0),
            # 分割信息
            "segment_source": "liteparse_segment",
            "table_id": st.get("table_id", -1),
            "is_cross_page": st.get("is_cross_page", False),
            "pages": st.get("pages", [page]),
            "caption": caption,
            "caption_info": st.get("caption_info", {}),
            "description_text": st.get("description_text", ""),
            "is_merged_adjacent": st.get("is_merged_adjacent", False),
            "is_split_from_mixed": st.get("is_split_from_mixed", False),
            "_cross_page_candidate": st.get("_cross_page_candidate", False),
            "_cross_page_ref_table_id": st.get("_cross_page_ref_table_id", -1),
            "_stripped_leading_rows": st.get("_stripped_leading_rows", []),
            "_stripped_tail_rows": st.get("_stripped_tail_rows", []),
            "confidence": st.get("confidence", 0),
            # liteparse 列 X 范围（用于 UI 渲染列间距提示）
            "column_x_ranges": column_x_ranges,
        }
        results.append(result)

    return results


# ================================================================
# 9. 便捷函数：打印验证报告
# ================================================================

def print_verification_report(tables: List[dict], report: dict):
    """将分割报告打印为可读文本。"""
    lines = []
    lines.append("═" * 60)
    lines.append("  liteparse 表格分割验证报告（纯规则驱动）")
    lines.append("═" * 60)
    lines.append(f"  表格页: {report.get('table_pages', 0)} / {report.get('total_pages', 0)} 页")
    lines.append(f"  分割表格数: {report.get('total_tables', 0)}")
    lines.append(f"  跨页拼接: {report.get('cross_page_merges', 0)} 处")
    lines.append("")

    # Phase 1 质量决策统计
    accepted_count = sum(1 for t in tables if t.get("quality_decision") == "accepted")
    review_count = sum(1 for t in tables if t.get("quality_decision") == "review")
    rejected_count = sum(1 for t in tables if t.get("quality_decision") == "rejected")
    lines.append(f"  质量决策: ✅ 可信 {accepted_count} | 🔍 待复核 {review_count} | ❌ 已拒绝 {rejected_count}")
    lines.append("")

    # 页级详情
    page_details = report.get("page_details", {})
    if page_details:
        lines.append("─" * 60)
        lines.append("  页级覆盖度")
        lines.append("─" * 60)
        for pg in sorted(page_details.keys()):
            d = page_details[pg]
            pct = (d["items_assigned"] / d["items_total"] * 100) if d["items_total"] > 0 else 0
            flag = " ⚠" if d["orphans"] > max(2, d["items_total"] * 0.05) else ""
            lines.append(
                f"  P{pg}: {d['items_assigned']}/{d['items_total']} items "
                f"({pct:.0f}%)  {d['tables']} 表  孤儿 {d['orphans']}{flag}"
            )

    # 孤儿详情
    orphan_items = report.get("orphan_page_items", {})
    if orphan_items:
        lines.append("")
        lines.append("─" * 60)
        lines.append("  ⚠ 孤儿 text_items（表格页上未被归属）")
        lines.append("─" * 60)
        for pg in sorted(orphan_items.keys()):
            for it in orphan_items[pg][:5]:
                lines.append(f"  P{pg}: '{it['text']}' (y={it['y0']:.0f})")
            if len(orphan_items[pg]) > 5:
                lines.append(f"  P{pg}: ... 共 {len(orphan_items[pg])} 个孤儿")

    # 表格摘要
    lines.append("")
    lines.append("─" * 60)
    lines.append("  表格摘要")
    lines.append("─" * 60)
    for ts in report.get("table_summaries", []):
        pages_str = f"P{ts['page']}" if len(ts['pages']) <= 1 else f"P{'-'.join(str(p) for p in ts['pages'])}"
        cross = " [跨页]" if ts.get("is_cross_page") else ""
        cap = f"「{ts['caption']}」" if ts.get("caption") else ""
        lines.append(f"  表#{ts['table_id']} ({pages_str}){cross} {cap}")
        lines.append(f"    行数: {ts['row_count']}, Items: {ts['items_count']}, 置信: {ts['confidence']:.2f}")
        # 质量决策标注
        t = tables[ts['table_id']] if ts['table_id'] < len(tables) else {}
        dec = t.get("quality_decision", "")
        dec_reason = t.get("quality_decision_reason", "")
        if dec:
            dec_icon = {"accepted": "✅", "review": "🔍", "rejected": "❌"}.get(dec, "")
            lines.append(f"    判定: {dec_icon} {dec} ({dec_reason})")
        first = " | ".join(ts.get("first_items", []))
        if first:
            lines.append(f"    首: {first}")
        last = " | ".join(ts.get("last_items", []))
        if last:
            lines.append(f"    末: {last}")

    lines.append("")
    lines.append("═" * 60)

    return "\n".join(lines)
