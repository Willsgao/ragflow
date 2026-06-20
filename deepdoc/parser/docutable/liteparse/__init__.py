# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 独立 PDF 解析模块

与 pdf2docx 通道并行，用 liteparse 的 Grid Projection 算法
逐页解析 PDF，保留空间布局文本。

使用示例:
    from codes.liteparse_extractor import LiteParseParser

    parser = LiteParseParser()

    # 全量解析
    result = parser.parse("report.pdf")

    # 只解析表格页（与 pdf2docx 对齐）
    result = parser.parse_table_pages_only("report.pdf", [5, 7, 12])

    # 从缓存加载
    result = parser.load_or_parse("report.pdf")

    # 按页访问
    page5 = result.get_page(5)
    print(page5.full_text)
    for region in page5.table_regions:
        print(region.region_text)

模块结构:
    config.py          — 配置常量
    models.py          — 数据模型 (TextItem, TableRegion, PageResult, ParseResult)
    page_processor.py  — 单页 liteparse 调用封装
    region_detector.py — 表格区域检测 (密度网格)
    cache_manager.py   — 中间数据持久化
    parser.py          — 主编排器
"""

from .config import LITEPARSE_CONFIG, FINANCIAL_KEYWORDS
from .models import TextItem, TableRegion, PageResult, ParseResult
from .parser import LiteParseParser
from .cache_manager import (
    save_parse_result,
    load_parse_result,
    is_cache_valid,
    delete_cache,
    get_cache_dir_path,
)

__all__ = [
    # 配置
    "LITEPARSE_CONFIG",
    "FINANCIAL_KEYWORDS",
    # 数据模型
    "TextItem",
    "TableRegion",
    "PageResult",
    "ParseResult",
    # 解析器
    "LiteParseParser",
    # 缓存
    "save_parse_result",
    "load_parse_result",
    "is_cache_valid",
    "delete_cache",
    "get_cache_dir_path",
]
