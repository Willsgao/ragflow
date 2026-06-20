# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 主编排器

职责：
1. 编排完整解析流水线：分页提取 → 表格区域检测 → 缓存
2. 对外提供统一入口：LiteParseParser
3. 支持按表格页筛选（只解析 pdf2docx 已标记的表格页）
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from .config import LITEPARSE_CONFIG
from .models import ParseResult, PageResult
from .page_processor import PageProcessor
from .region_detector import RegionDetector
from . import cache_manager as cache

logger = logging.getLogger(__name__)


class LiteParseParser:
    """liteparse 解析主编排器。

    用法:
        parser = LiteParseParser()
        result = parser.parse("report.pdf")

        # 只解析指定页码（如 pdf2docx 已标记的表格页）
        result = parser.parse("report.pdf", target_pages=[5, 7, 12])

        # 从缓存加载
        result = parser.load_or_parse("report.pdf")
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or LITEPARSE_CONFIG
        self.page_processor = PageProcessor(self.cfg)
        self.region_detector = RegionDetector(self.cfg)

    # ================================================================
    # 公开接口
    # ================================================================

    def parse(
        self,
        pdf_path: str,
        target_pages: Optional[List[int]] = None,
        detect_regions: bool = True,
        force: bool = False,
    ) -> ParseResult:
        """解析 PDF，返回标准化的 ParseResult。

        Args:
            pdf_path:       PDF 文件路径
            target_pages:   要解析的页码列表（1-based），None = 全部
            detect_regions: 是否检测表格区域
            force:          是否强制重新解析（忽略缓存）

        Returns:
            ParseResult（按页组织）

        Raises:
            FileNotFoundError: PDF 文件不存在
            RuntimeError:      liteparse 不可用
        """
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

        # 尝试加载缓存
        if not force and cache.is_cache_valid(pdf_path):
            cached = cache.load_parse_result(pdf_path)
            if cached is not None:
                logger.info(f"从缓存加载 liteparse 结果: {pdf_path}")
                return cached

        t_start = time.time()

        # 获取 PDF 总页数
        total_pages = self._get_page_count(pdf_path)

        # 逐页解析
        logger.info(
            f"liteparse 开始解析: {pdf_path} "
            f"(总 {total_pages} 页, 目标 {len(target_pages) if target_pages else total_pages} 页)"
        )
        pages = self.page_processor.process_all_pages(
            pdf_path, total_pages, target_pages
        )

        # 表格区域检测
        if detect_regions:
            logger.info("检测表格区域...")
            pages = self.region_detector.detect_all(pages)
            n_table = sum(1 for p in pages if p.is_table_page)
            logger.info(f"表格区域检测完成: {n_table} / {len(pages)} 页包含表格")

        # 对未解析的页补充空结果（保证 total_pages 完整）
        parsed_nums = {p.page_number for p in pages}
        if target_pages is None:
            # 全量解析时，补充 liteparse 可能跳过的页
            for pn in range(1, total_pages + 1):
                if pn not in parsed_nums:
                    pages.append(PageResult(
                        page_number=pn,
                        page_width=0, page_height=0,
                        error="liteparse 未返回此页数据",
                    ))

        # 组装结果
        pages.sort(key=lambda p: p.page_number)
        result = ParseResult(
            pdf_path=pdf_path,
            total_pages=total_pages,
            pages=pages,
            parse_time_sec=round(time.time() - t_start, 2),
        )

        # 保存中间数据
        cache_dir = cache.save_parse_result(result)
        logger.info(
            f"liteparse 解析完成: "
            f"{result.page_count_with_table}/{result.total_pages} 页含表格, "
            f"耗时 {result.parse_time_sec:.1f}s, "
            f"缓存 → {cache_dir}"
        )

        return result

    def load_or_parse(
        self,
        pdf_path: str,
        target_pages: Optional[List[int]] = None,
        detect_regions: bool = True,
    ) -> ParseResult:
        """优先从缓存加载，缓存失效则重新解析。"""
        return self.parse(
            pdf_path, target_pages, detect_regions, force=False
        )

    def parse_table_pages_only(
        self,
        pdf_path: str,
        table_page_numbers: List[int],
    ) -> ParseResult:
        """只解析已知的表格页（与 pdf2docx 结果对齐）。

        Args:
            pdf_path:             PDF 路径
            table_page_numbers:   pdf2docx 已标记的表格页页码列表

        这是差分对比前的关键预处理步骤：
        只对 pdf2docx 识别为表格的页做 liteparse 解析，
        大幅减少 token 消耗。
        """
        if not table_page_numbers:
            logger.warning("table_page_numbers 为空，跳过 liteparse 解析")
            return ParseResult(
                pdf_path=pdf_path,
                total_pages=self._get_page_count(pdf_path),
            )
        return self.parse(pdf_path, target_pages=sorted(table_page_numbers))

    # ================================================================
    # 工具
    # ================================================================

    @staticmethod
    def _get_page_count(pdf_path: str) -> int:
        """获取 PDF 总页数（优先用 PyMuPDF，降级用 pdfplumber）。"""
        try:
            import fitz
            doc = fitz.open(pdf_path)
            count = doc.page_count
            doc.close()
            return count
        except Exception:
            pass
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                return len(pdf.pages)
        except Exception:
            pass
        return 0

    @staticmethod
    def is_available() -> bool:
        """检查 liteparse 是否可用。"""
        from .page_processor import _check_liteparse_python, _check_liteparse_cli
        return _check_liteparse_python() or _check_liteparse_cli()
