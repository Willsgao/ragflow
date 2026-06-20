# -*- coding: utf-8 -*-
"""
Table Validator — 数据模型

定义交叉验证过程中使用的结构化数据类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


# ============================================================
# 真假表格分类结果
# ============================================================

@dataclass
class ClassifyResult:
    """单页表格分类结果"""
    page: int                                    # 1-based 页码
    is_real_table: bool                          # 是否判定为真表格
    confidence: float = 1.0                      # 置信度 0~1

    # 分类依据
    reason: str = ""                             # 判定理由
    checks: dict = field(default_factory=dict)   # 各项检查明细
    # {
    #   "has_numeric_col": bool,
    #   "is_toc": bool,
    #   "has_enough_rows": bool,
    #   "has_enough_cols": bool,
    # }

    # 增强字段：表格质量分类（Phase 4 优化新增）
    is_complete: bool = True                     # 表格是否完整（有表头+数据+可能汇总行）
    table_category: str = ""                     # 分类标签："财务数据表"/"文本列表"/"目录"/"图表标签"/"混合表"
    has_header: bool = False                     # 是否有表头行
    has_numeric_data: bool = False               # 是否有数值数据列
    segment_source: str = ""                     # 分割来源："liteparse_segment"/"original"

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "is_real_table": self.is_real_table,
            "confidence": self.confidence,
            "reason": self.reason,
            "checks": self.checks,
            "is_complete": self.is_complete,
            "table_category": self.table_category,
            "has_header": self.has_header,
            "has_numeric_data": self.has_numeric_data,
            "segment_source": self.segment_source,
        }


# ============================================================
# LLM 验证结果（5 个维度）
# ============================================================

@dataclass
class LLMVerifyResult:
    """LLM 对单张表格的验证结果"""
    page: int = 0

    # 1. 表头判断
    header_correct: bool = True                  # 表头是否正确
    header_issues: List[str] = field(default_factory=list)
    # e.g. ["表头行疑似缺失", "第一行不是表头，是数据行"]

    # 2. 重复判断
    has_duplicate_header: bool = False           # 表头在数据行中重复
    has_duplicate_data: bool = False             # 数据行重复
    duplicate_details: List[str] = field(default_factory=list)
    # e.g. ["第3行与第7行数据完全相同", "表头行在第5行重复出现"]

    # 3. 错位判断
    has_misalignment: bool = False
    misalignment_details: List[str] = field(default_factory=list)
    # e.g. ["第2列从行6开始整体右移了1列", "数值列出现文本、文本列出现数值"]

    # 4. 底部混入文本
    has_footer_text: bool = False
    footer_from_row: int = -1                    # 从第几行开始是混入文本（0-based）
    footer_details: List[str] = field(default_factory=list)
    # e.g. ["最后2行是页脚说明文字，应删除"]

    # 5. 拼接判断
    needs_merge_prev: bool = False               # 需要与上一页拼接
    needs_merge_next: bool = False               # 需要与下一页拼接
    merge_details: List[str] = field(default_factory=list)
    # e.g. ["表头缺失，疑似前一页的续行", "最后一行数据不完整"]

    # LLM 原始回复 + 元数据
    llm_raw_response: str = ""
    llm_error: Optional[str] = None
    usage: Optional[dict] = None                 # token 消耗

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "header_correct": self.header_correct,
            "header_issues": self.header_issues,
            "has_duplicate_header": self.has_duplicate_header,
            "has_duplicate_data": self.has_duplicate_data,
            "duplicate_details": self.duplicate_details,
            "has_misalignment": self.has_misalignment,
            "misalignment_details": self.misalignment_details,
            "has_footer_text": self.has_footer_text,
            "footer_from_row": self.footer_from_row,
            "footer_details": self.footer_details,
            "needs_merge_prev": self.needs_merge_prev,
            "needs_merge_next": self.needs_merge_next,
            "merge_details": self.merge_details,
            "llm_error": self.llm_error,
            "usage": self.usage,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LLMVerifyResult":
        return cls(
            page=d.get("page", 0),
            header_correct=d.get("header_correct", True),
            header_issues=d.get("header_issues", []),
            has_duplicate_header=d.get("has_duplicate_header", False),
            has_duplicate_data=d.get("has_duplicate_data", False),
            duplicate_details=d.get("duplicate_details", []),
            has_misalignment=d.get("has_misalignment", False),
            misalignment_details=d.get("misalignment_details", []),
            has_footer_text=d.get("has_footer_text", False),
            footer_from_row=d.get("footer_from_row", -1),
            footer_details=d.get("footer_details", []),
            needs_merge_prev=d.get("needs_merge_prev", False),
            needs_merge_next=d.get("needs_merge_next", False),
            merge_details=d.get("merge_details", []),
            llm_error=d.get("llm_error"),
            usage=d.get("usage"),
        )


# ============================================================
# 交叉验证总体结果
# ============================================================

@dataclass
class CrossValidateReport:
    """整份 PDF 的交叉验证报告"""
    pdf_path: str = ""

    # 分类结果（所有被 pdf2docx 识别为表格的页）
    classify_results: List[ClassifyResult] = field(default_factory=list)

    # LLM 验证结果（仅真表格页）
    llm_results: List[LLMVerifyResult] = field(default_factory=list)

    # 统计
    total_pages: int = 0
    real_table_count: int = 0
    fake_table_count: int = 0
    verified_count: int = 0

    # 错误
    error: Optional[str] = None

    def __post_init__(self):
        self.real_table_count = sum(1 for r in self.classify_results if r.is_real_table)
        self.fake_table_count = sum(1 for r in self.classify_results if not r.is_real_table)
        self.verified_count = len(self.llm_results)

    @property
    def has_issues(self) -> bool:
        """是否有任意问题被检出"""
        for r in self.llm_results:
            if (not r.header_correct or r.has_duplicate_header
                    or r.has_duplicate_data or r.has_misalignment
                    or r.has_footer_text or r.needs_merge_prev
                    or r.needs_merge_next):
                return True
        return False

    def summary(self) -> str:
        lines = [
            f"PDF: {self.pdf_path}",
            f"总页数: {self.total_pages}",
            f"真表格: {self.real_table_count}  假表格: {self.fake_table_count}",
            f"LLM 验证: {self.verified_count} 页",
        ]
        if self.error:
            lines.append(f"错误: {self.error}")
        issue_count = sum(
            1 for r in self.llm_results
            if (not r.header_correct or r.has_duplicate_header
                or r.has_duplicate_data or r.has_misalignment
                or r.has_footer_text or r.needs_merge_prev
                or r.needs_merge_next)
        )
        lines.append(f"发现问题: {issue_count} 页")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "pdf_path": self.pdf_path,
            "classify_results": [r.to_dict() for r in self.classify_results],
            "llm_results": [r.to_dict() for r in self.llm_results],
            "total_pages": self.total_pages,
            "real_table_count": self.real_table_count,
            "fake_table_count": self.fake_table_count,
            "verified_count": self.verified_count,
            "error": self.error,
        }
