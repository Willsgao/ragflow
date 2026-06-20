# -*- coding: utf-8 -*-
"""
Table Validator — 模块入口

交叉验证模块：
- 规则分类器：判断每一页 PDF 是否包含真正的数据表格
- LLM 验证器：对真表格页调用 LLM 进行 5 维度深度验证
- 规则修复器：自底向上分层表头结构修复（纯规则，零LLM调用）
"""

from .config import CLASSIFIER_CONFIG, LLM_CONFIG, TOC_KEYWORDS
from .models import (
    ClassifyResult,
    LLMVerifyResult,
    CrossValidateReport,
)
from .table_classifier import classify_page, classify_all
from .llm_checker import verify_table_with_llm
from .validator import run_cross_validation, quick_classify
from .rule_based_repair import (
    repair_table_rules,
    repair_and_split_tables,
    generate_rules_repair_report,
    RepairAnomaly,
    has_high_severity_anomalies,
    has_medium_or_higher_anomalies,
    prepare_anomalies_for_llm,
    ANCHOR_SHIFT,
    WEAK_ANCHOR,
    HEADER_TEXT_MISSING,
    DATA_HEADER_MISMATCH,
    ORPHAN_HEADER_TEXT,
    TRUNCATED_HEADER_MERGED,
    ANOMALY_LOW,
    ANOMALY_MEDIUM,
    ANOMALY_HIGH,
)

__all__ = [
    # 配置
    "CLASSIFIER_CONFIG",
    "LLM_CONFIG",
    "TOC_KEYWORDS",
    # 模型
    "ClassifyResult",
    "LLMVerifyResult",
    "CrossValidateReport",
    # 分类器
    "classify_page",
    "classify_all",
    # LLM
    "verify_table_with_llm",
    # 编排
    "run_cross_validation",
    "quick_classify",
    # 规则修复
    "repair_table_rules",
    "repair_and_split_tables",
    "generate_rules_repair_report",
    "RepairAnomaly",
    "has_high_severity_anomalies",
    "has_medium_or_higher_anomalies",
    "prepare_anomalies_for_llm",
    # 异常类型常量
    "ANCHOR_SHIFT",
    "WEAK_ANCHOR",
    "HEADER_TEXT_MISSING",
    "DATA_HEADER_MISMATCH",
    "ORPHAN_HEADER_TEXT",
    "TRUNCATED_HEADER_MERGED",
    "ANOMALY_LOW",
    "ANOMALY_MEDIUM",
    "ANOMALY_HIGH",
]
