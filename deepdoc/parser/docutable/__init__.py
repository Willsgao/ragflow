# -*- coding: utf-8 -*-
"""
DocuTable — PDF 表格解析包

流水线：
    core/        → PDF 文本提取 + TableProcessor 表格结构重建 + Excel/JSON 导出
    liteparse/   → liteparse 空间解析通道 + 表格区域检测 + 缓存管理
    validator/   → 规则分类 + LLM 5维度验证 + 差异修复

使用示例:
    # 仅核心提取
    from deepdoc.parser.docutable.core import PDFExtractor
    ext = PDFExtractor()  # check_keywords=False 处理所有页面
    results = ext.extract("report.pdf")

    # 带 TableProcessor 重建
    from deepdoc.parser.docutable.core import TableProcessor, GapDetector
"""

from .core import (
    PDFExtractor,
    TableProcessor,
    ColumnAnalyzer,
    GapDetector,
    ExcelExporter,
)

__all__ = [
    "PDFExtractor",
    "TableProcessor",
    "ColumnAnalyzer",
    "GapDetector",
    "ExcelExporter",
]
