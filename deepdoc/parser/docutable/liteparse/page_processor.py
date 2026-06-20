# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 单页处理器

职责：
1. 封装 liteparse 调用（Python API，liteparse >= 2.0）
2. 将 liteparse 输出转换为本模块的通用数据模型（PageResult）
3. 批量解析优化：多页合并为一次 liteparse 调用
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .config import LITEPARSE_CONFIG
from .models import PageResult, TextItem

logger = logging.getLogger(__name__)

# ============================================================
# liteparse 可用性检测
# ============================================================

_liteparse_available: Optional[bool] = None


def _check_liteparse_python() -> bool:
    """检测 liteparse Python 包是否可用。"""
    global _liteparse_available
    if _liteparse_available is not None:
        return _liteparse_available
    try:
        import liteparse  # noqa: F401
        _liteparse_available = True
    except ImportError:
        _liteparse_available = False
    return _liteparse_available


# ============================================================
# 页码 → target_pages 字符串
# ============================================================

def _pages_to_target_str(page_numbers: List[int]) -> str:
    """将页码列表转为 liteparse target_pages 格式，如 "1-5,10,15-20"。

    自动合并连续页码为范围表示，减少字符串长度。
    """
    if not page_numbers:
        return ""
    sorted_pages = sorted(set(page_numbers))
    parts: List[str] = []
    start = sorted_pages[0]
    end = sorted_pages[0]
    for p in sorted_pages[1:]:
        if p == end + 1:
            end = p
        else:
            parts.append(str(start) if start == end else f"{start}-{end}")
            start = end = p
    parts.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(parts)


# ============================================================
# 单页 / 批量处理
# ============================================================

class PageProcessor:
    """liteparse 单页 / 批量处理器。

    封装 liteparse >= 2.0 的调用细节，输出标准化的 PageResult。

    优化策略：
    - process_all_pages() 将所有目标页合并为一次 liteparse 调用
    - process_page() 单页调用（内部也是 target_pages="N"）
    """

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or LITEPARSE_CONFIG

    # ---- 公开接口 ----

    def process_page(self, pdf_path: str, page_num: int) -> PageResult:
        """解析 PDF 的指定页（1-based），返回标准化的 PageResult。"""
        if not _check_liteparse_python():
            raise RuntimeError(
                "liteparse 不可用。请安装：pip install liteparse"
            )
        results = self._parse_pages(pdf_path, [page_num])
        if not results:
            raise ValueError(f"liteparse 未返回第 {page_num} 页的数据")
        return results[0]

    def process_all_pages(
        self, pdf_path: str, total_pages: int,
        target_pages: Optional[List[int]] = None,
    ) -> List[PageResult]:
        """批量解析，可按需指定页码。

        将所有目标页合并为一次 liteparse 调用，而非逐页调用，
        大幅减少 liteparse 初始化开销。

        Args:
            pdf_path:     PDF 文件路径
            total_pages:  PDF 总页数（用于 target_pages=None 时遍历全部）
            target_pages: 要解析的页码列表（1-based），None 表示前 max_pages 页

        Returns:
            按页码排序的 PageResult 列表
        """
        if not _check_liteparse_python():
            raise RuntimeError(
                "liteparse 不可用。请安装：pip install liteparse"
            )

        pages_to_parse = target_pages or list(
            range(1, min(total_pages, self.cfg.get("max_pages", 500)) + 1)
        )
        if not pages_to_parse:
            return []

        logger.info(
            f"liteparse 批量解析: {len(pages_to_parse)} 页 "
            f"({pages_to_parse[0]}~{pages_to_parse[-1]})"
        )

        results = self._parse_pages(pdf_path, pages_to_parse)

        # 补充未返回的页（liteparse 可能因无文本跳过某些页）
        returned_nums = {p.page_number for p in results}
        for pn in pages_to_parse:
            if pn not in returned_nums:
                results.append(PageResult(
                    page_number=pn,
                    page_width=0, page_height=0,
                    error="liteparse 未返回此页数据",
                ))

        results.sort(key=lambda p: p.page_number)
        return results

    # ---- 核心调用 ----

    def _parse_pages(
        self, pdf_path: str, page_numbers: List[int]
    ) -> List[PageResult]:
        """一次 liteparse 调用解析多个指定页。"""
        import liteparse

        target_str = _pages_to_target_str(page_numbers)
        parser = liteparse.LiteParse(
            target_pages=target_str,
            max_pages=self.cfg.get("max_pages", 500),
            ocr_enabled=False,   # 文本型 PDF 无需 OCR
        )
        result = parser.parse(pdf_path)

        # 转换每一页
        page_map = {
            p.page_num: self._from_liteparse_page(p)
            for p in (result.pages or [])
        }

        # 按请求顺序返回
        return [page_map[pn] for pn in page_numbers if pn in page_map]

    # ---- 数据转换 ----

    @staticmethod
    def _from_liteparse_page(lp_page) -> PageResult:
        """liteparse ParsedPage → 本模块 PageResult。

        liteparse >= 2.0 TextItem 结构:
            .text: str
            .x, .y: float          (左上角)
            .width, .height: float  (宽高)
            .font_name: Optional[str]
            .font_size: Optional[float]
            .confidence: Optional[float]
        """
        text_items = []
        for item in (lp_page.text_items or []):
            text_items.append(TextItem(
                text=str(item.text or ""),
                x0=float(item.x or 0),
                y0=float(item.y or 0),
                x1=float((item.x or 0) + (item.width or 0)),
                y1=float((item.y or 0) + (item.height or 0)),
                font_size=float(item.font_size or 0),
                font_name=str(item.font_name or ""),
            ))

        return PageResult(
            page_number=int(lp_page.page_num),
            page_width=float(lp_page.width or 0),
            page_height=float(lp_page.height or 0),
            full_text=str(lp_page.text or ""),
            text_items=text_items,
        )
