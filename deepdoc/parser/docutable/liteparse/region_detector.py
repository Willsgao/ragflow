# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 表格区域检测器

在 liteparse 解析后的单页结果上，
用文本密度网格定位表格区域的物理坐标，
并提取区域文本和上下文文本。

算法与 V2-Lite (page_assigner.py) 保持一致：
  关键词预筛 → 密度网格 → 连续行合并 → 上下文提取
"""

from __future__ import annotations

from typing import List, Tuple

from .config import LITEPARSE_CONFIG, FINANCIAL_KEYWORDS
from .models import PageResult, TableRegion


class RegionDetector:
    """表格区域检测器。

    在 liteparse 的 TextItem 列表上运行密度网格算法，
    不依赖 PyMuPDF / pdfplumber。
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or LITEPARSE_CONFIG
        self.keywords = FINANCIAL_KEYWORDS

    # ================================================================
    # 公开接口
    # ================================================================

    def detect(self, page: PageResult) -> PageResult:
        """对单页结果检测表格区域，原地标记 is_table_page 并填充 table_regions。

        Returns:
            同一个 PageResult（已原地修改）
        """
        if not page.text_items or page.error:
            return page

        # 步骤 1：关键词预筛
        full_text = " ".join(w.text for w in page.text_items)
        if not self._has_financial_keywords(full_text):
            page.is_table_page = False
            return page

        # 步骤 2：密度网格检测
        density_rows = self._build_density_grid(page)
        table_row_groups = self._find_table_rows(density_rows)

        if not table_row_groups:
            page.is_table_page = False
            return page

        # 步骤 3：合并连续表格行为物理区域
        table_regions_rects = self._merge_to_regions(
            page, density_rows, table_row_groups
        )

        # 步骤 4：提取区域文本 + 上下文
        for (rx0, ry0, rx1, ry1) in table_regions_rects:
            region_text = self._extract_region_text(page, rx0, ry0, rx1, ry1)
            context_text = self._extract_context(page, rx0, ry0, rx1, ry1)
            confidence = self._calc_confidence(page, rx0, ry0, rx1, ry1)

            page.table_regions.append(TableRegion(
                x0=rx0, y0=ry0, x1=rx1, y1=ry1,
                region_text=region_text,
                context_text=context_text,
                confidence=confidence,
            ))

        page.is_table_page = len(page.table_regions) > 0
        return page

    def detect_all(self, pages: List[PageResult]) -> List[PageResult]:
        """批量检测，返回原地修改后的列表。"""
        for p in pages:
            self.detect(p)
        return pages

    # ================================================================
    # 关键词预筛
    # ================================================================

    def _has_financial_keywords(self, full_text: str) -> bool:
        return any(kw in full_text for kw in self.keywords)

    # ================================================================
    # 密度网格
    # ================================================================

    def _build_density_grid(self, page: PageResult) -> List[int]:
        """构建文本密度网格，返回每行的 word 计数。

        10x10 网格 → 统计每个单元格的 word 数 → 按行求和。
        """
        grid_rows = self.cfg["density_grid"]
        grid_cols = self.cfg["density_grid"]
        if page.page_height <= 0 or page.page_width <= 0:
            return [0] * grid_rows

        cell_h = page.page_height / grid_rows
        cell_w = page.page_width / grid_cols
        density = [[0] * grid_cols for _ in range(grid_rows)]

        for w in page.text_items:
            cx = w.center_x
            cy = w.center_y
            col = int(cx / cell_w) if cell_w > 0 else 0
            row = int(cy / cell_h) if cell_h > 0 else 0
            if 0 <= row < grid_rows and 0 <= col < grid_cols:
                density[row][col] += 1

        return [sum(density[r]) for r in range(grid_rows)]

    def _find_table_rows(self, row_density: List[int]) -> List[List[int]]:
        """找出密度超过阈值的行，合并连续行为组。"""
        if not row_density:
            return []

        avg = sum(row_density) / max(len(row_density), 1)
        avg = max(avg, 3)  # 至少 3，避免噪声

        threshold = avg * self.cfg["density_threshold"]

        # 标记表格行
        table_indices = [
            r for r in range(len(row_density))
            if row_density[r] > threshold
        ]

        # 合并连续行
        if not table_indices:
            return []

        groups = []
        current_group = [table_indices[0]]
        for i in range(1, len(table_indices)):
            if table_indices[i] == table_indices[i - 1] + 1:
                current_group.append(table_indices[i])
            else:
                groups.append(current_group)
                current_group = [table_indices[i]]
        groups.append(current_group)
        return groups

    # ================================================================
    # 区域合并
    # ================================================================

    def _merge_to_regions(
        self, page: PageResult,
        density_rows: List[int],
        table_groups: List[List[int]],
    ) -> List[Tuple[float, float, float, float]]:
        """将密度行组映射为页面物理坐标区域。"""
        grid_rows = self.cfg["density_grid"]
        if page.page_height <= 0:
            return []

        cell_h = page.page_height / grid_rows
        page_w = page.page_width
        min_w = page_w * self.cfg["table_min_width_ratio"]
        min_h = self.cfg["table_min_height"]

        regions: List[Tuple[float, float, float, float]] = []
        for group in table_groups:
            grid_y0 = group[0]
            grid_y1 = group[-1] + 1  # 网格行区间 [grid_y0, grid_y1)
            ry0 = grid_y0 * cell_h
            ry1 = grid_y1 * cell_h

            # 在区域内找实际文字边界
            rx0, rx1_actual = self._find_text_horizontal_bounds(
                page, ry0, ry1
            )

            region_w = rx1_actual - rx0
            region_h = ry1 - ry0

            if region_w >= min_w and region_h >= min_h:
                # 左右各留一点余量
                margin = 10.0
                rx0 = max(0, rx0 - margin)
                rx1_actual = min(page_w, rx1_actual + margin)
                regions.append((rx0, ry0, rx1_actual, ry1))

        return regions

    def _find_text_horizontal_bounds(
        self, page: PageResult, y0: float, y1: float
    ) -> Tuple[float, float]:
        """在给定的 Y 区间内，找到文字的 X 边界。"""
        min_x = page.page_width
        max_x = 0.0
        for w in page.text_items:
            if y0 <= w.center_y <= y1:
                if w.x0 < min_x:
                    min_x = w.x0
                if w.x1 > max_x:
                    max_x = w.x1
        if min_x >= max_x:
            return 0.0, page.page_width
        return min_x, max_x

    # ================================================================
    # 区域文本提取
    # ================================================================

    def _extract_region_text(
        self, page: PageResult,
        rx0: float, ry0: float, rx1: float, ry1: float,
    ) -> str:
        """提取表格区域内的所有文本（保留顺序）。"""
        region_words = []
        for w in page.text_items:
            if (rx0 <= w.center_x <= rx1
                    and ry0 <= w.center_y <= ry1
                    and w.text.strip()):
                region_words.append(w)
        # 按 Y 优先排序（从上到下、从左到右）
        region_words.sort(key=lambda w: (w.center_y, w.center_x))
        return " ".join(w.text for w in region_words)

    def _extract_context(
        self, page: PageResult,
        rx0: float, ry0: float, rx1: float, ry1: float,
    ) -> str:
        """提取表格区域上方的上下文文本（如表格标题）。

        取 ry0 上方 context_margin_top 范围内的文字，
        排除已被其他表格区域占用的部分。
        """
        margin = self.cfg["context_margin_top"]
        ctx_y0 = max(0, ry0 - margin)
        ctx_y1 = ry0

        # 宽范围取文字，中心点在表格区域水平范围内
        ctx_words = []
        for w in page.text_items:
            if not w.text.strip():
                continue
            if ctx_y0 <= w.center_y <= ctx_y1:
                # X 范围放宽：表格区域的 0.5x ~ 1.5x
                x_mid = (rx0 + rx1) / 2
                half_w = (rx1 - rx0) * 0.75
                if abs(w.center_x - x_mid) <= half_w:
                    ctx_words.append(w)

        # 排除落在表格区域内的词
        ctx_filtered = []
        for w in ctx_words:
            if not self._inside_any_table_region(page, w):
                ctx_filtered.append(w)

        ctx_filtered.sort(key=lambda w: (w.center_y, w.center_x))
        return " ".join(w.text for w in ctx_filtered)

    def _inside_any_table_region(
        self, page: PageResult, item
    ) -> bool:
        """检查 TextItem 是否落在已有的表格区域内。"""
        for tr in page.table_regions:
            if (tr.x0 * 0.9 <= item.center_x <= tr.x1 * 1.1
                    and tr.y0 * 0.9 <= item.center_y <= tr.y1 * 1.1):
                return True
        return False

    # ================================================================
    # 置信度评估
    # ================================================================

    def _calc_confidence(
        self, page: PageResult,
        rx0: float, ry0: float, rx1: float, ry1: float,
    ) -> float:
        """根据区域特征估算表格检测置信度 (0~1)。"""
        score = 0.5  # 基础分

        # 宽度足够 → +分数
        ratio = (rx1 - rx0) / max(page.page_width, 1)
        if ratio >= 0.5:
            score += 0.2
        elif ratio >= 0.3:
            score += 0.1

        # 区域内文字密度 → +分数
        word_count = sum(
            1 for w in page.text_items
            if rx0 <= w.center_x <= rx1
            and ry0 <= w.center_y <= ry1
            and w.text.strip()
        )
        if word_count >= 20:
            score += 0.2
        elif word_count >= 10:
            score += 0.1

        # 有上下文文字（标题）→ +分数
        if self._extract_context(page, rx0, ry0, rx1, ry1).strip():
            score += 0.1

        return min(score, 1.0)
