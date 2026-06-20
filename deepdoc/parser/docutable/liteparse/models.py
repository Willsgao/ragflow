# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 数据模型

定义解析过程中使用的结构化数据类，
方便后续差分对比模块直接消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ============================================================
# 基础原子结构
# ============================================================

@dataclass
class TextItem:
    """PDF 页面上一个独立的文本片段（含坐标）。

    对应 liteparse 返回的每个 text_item：
    - text:  文本内容
    - bbox:  包围盒 (x0, y0, x1, y1)，单位 pt，左上角原点
    """
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float = 0.0
    font_name: str = ""

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "font_size": round(self.font_size, 2),
            "font_name": self.font_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TextItem":
        return cls(
            text=d["text"],
            x0=d["x0"], y0=d["y0"], x1=d["x1"], y1=d["y1"],
            font_size=d.get("font_size", 0.0),
            font_name=d.get("font_name", ""),
        )


# ============================================================
# 表格区域
# ============================================================

@dataclass
class TableRegion:
    """页面上一个被检测到的表格候选区域。"""
    x0: float
    y0: float
    x1: float
    y1: float
    region_text: str = ""              # 区域内的拼接文本
    context_text: str = ""             # 区域上方的上下文文本（如表格标题）
    confidence: float = 0.0            # 置信度 0~1

    def to_dict(self) -> dict:
        return {
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "region_text": self.region_text,
            "context_text": self.context_text,
            "confidence": round(self.confidence, 4),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TableRegion":
        return cls(
            x0=d["x0"], y0=d["y0"], x1=d["x1"], y1=d["y1"],
            region_text=d.get("region_text", ""),
            context_text=d.get("context_text", ""),
            confidence=d.get("confidence", 0.0),
        )


# ============================================================
# 单页结果
# ============================================================

@dataclass
class PageResult:
    """单页 liteparse 解析结果。

    包含：
    - 页面元数据（尺寸、页码）
    - 全部文本片段（含坐标）
    - 保留版式的全文文本
    - 检测到的表格区域列表
    """
    page_number: int                         # 1-based
    page_width: float                        # pt
    page_height: float                       # pt
    full_text: str = ""                      # 保留空格对齐的版式文本
    text_items: List[TextItem] = field(default_factory=list)
    table_regions: List[TableRegion] = field(default_factory=list)
    is_table_page: bool = False              # 是否可能包含表格
    error: Optional[str] = None              # 本页解析错误信息

    @property
    def table_texts(self) -> List[str]:
        """便捷属性：所有表格区域的 region_text 列表。"""
        return [r.region_text for r in self.table_regions if r.region_text.strip()]

    def to_dict(self) -> dict:
        return {
            "page_number": self.page_number,
            "page_width": round(self.page_width, 2),
            "page_height": round(self.page_height, 2),
            "full_text": self.full_text,
            "text_items": [ti.to_dict() for ti in self.text_items],
            "table_regions": [tr.to_dict() for tr in self.table_regions],
            "is_table_page": self.is_table_page,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PageResult":
        return cls(
            page_number=d["page_number"],
            page_width=d["page_width"],
            page_height=d["page_height"],
            full_text=d.get("full_text", ""),
            text_items=[TextItem.from_dict(ti) for ti in d.get("text_items", [])],
            table_regions=[TableRegion.from_dict(tr) for tr in d.get("table_regions", [])],
            is_table_page=d.get("is_table_page", False),
            error=d.get("error"),
        )


# ============================================================
# 解析总体结果
# ============================================================

@dataclass
class ParseResult:
    """整份 PDF 的 liteparse 解析结果。

    按页组织，支持按页码快速检索。
    """
    pdf_path: str
    total_pages: int
    pages: List[PageResult] = field(default_factory=list)
    parse_time_sec: float = 0.0
    error: Optional[str] = None

    # ---- 内部索引 ----
    _page_index: Dict[int, PageResult] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._rebuild_index()

    def _rebuild_index(self):
        self._page_index = {p.page_number: p for p in self.pages}

    def get_page(self, page_num: int) -> Optional[PageResult]:
        """按 1-based 页码获取单页结果。"""
        return self._page_index.get(page_num)

    @property
    def table_pages(self) -> List[PageResult]:
        """仅返回包含表格的页。"""
        return [p for p in self.pages if p.is_table_page]

    @property
    def table_page_numbers(self) -> List[int]:
        """表格页的页码列表（排序）。"""
        return sorted(p.page_number for p in self.table_pages)

    @property
    def page_count_with_table(self) -> int:
        return len(self.table_pages)

    @property
    def error_pages(self) -> List[PageResult]:
        """解析出错的页。"""
        return [p for p in self.pages if p.error]

    def to_dict(self) -> dict:
        return {
            "pdf_path": self.pdf_path,
            "total_pages": self.total_pages,
            "parse_time_sec": round(self.parse_time_sec, 2),
            "error": self.error,
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ParseResult":
        result = cls(
            pdf_path=d["pdf_path"],
            total_pages=d["total_pages"],
            pages=[PageResult.from_dict(p) for p in d.get("pages", [])],
            parse_time_sec=d.get("parse_time_sec", 0.0),
            error=d.get("error"),
        )
        result._rebuild_index()
        return result

    def summary(self) -> str:
        """人类可读的解析摘要。"""
        lines = [
            f"PDF: {self.pdf_path}",
            f"总页数: {self.total_pages}",
            f"解析耗时: {self.parse_time_sec:.1f}s",
            f"表格页: {self.page_count_with_table} / {self.total_pages}",
            f"错误页: {len(self.error_pages)}",
        ]
        if self.table_page_numbers:
            lines.append(f"表格页码: {self.table_page_numbers}")
        if self.error:
            lines.append(f"全局错误: {self.error}")
        return "\n".join(lines)
