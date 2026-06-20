# -*- coding: utf-8 -*-
"""
DocuTable Core — PDF 表格提取核心引擎

    PDFExtractor   — pdfplumber + PyMuPDF 双引擎 PDF 文本提取
    TableProcessor — 基于间隙检测/聚类的表格结构重建
    GapDetector    — 自适应列边界检测
    ColumnAnalyzer — K-Means 聚类列分析
    ExcelExporter  — Excel/JSON 导出
"""

from .extractor import PDFExtractor
from .table_processor import TableProcessor
from .gap_detector import GapDetector
from .column_analyzer import ColumnAnalyzer
from .exporter import ExcelExporter

__all__ = [
    "PDFExtractor",
    "TableProcessor",
    "GapDetector",
    "ColumnAnalyzer",
    "ExcelExporter",
]
