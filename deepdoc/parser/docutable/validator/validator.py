# -*- coding: utf-8 -*-
"""
Table Validator — 主编排器

职责:
1. 加载 liteparse 缓存数据
2. 对 pdf2docx 表格页逐页分类（真/假表格）
3. 对真表格页调用 LLM 进行 5 维度验证
4. 汇总生成交叉验证报告
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from .config import VALIDATION_CACHE_VERSION
from .models import (
    ClassifyResult,
    LLMVerifyResult,
    CrossValidateReport,
)
from .table_classifier import classify_all, classify_page
from .llm_checker import verify_table_with_llm

logger = logging.getLogger(__name__)


def _load_liteparse_pages(pdf_path: str) -> Optional[dict]:
    """加载 liteparse 缓存中的 pages.json 数据

    Returns:
        dict 有 'pages' 列表，或 None
    """
    from ..liteparse.cache_manager import load_parse_result

    try:
        result = load_parse_result(pdf_path)
        if result is not None:
            return result.to_dict()
    except Exception as e:
        logger.warning(f"加载 liteparse 缓存失败: {e}")
    return None


def _get_liteparse_page(liteparse_data: dict, page_num: int) -> Optional[dict]:
    """从 liteparse 数据中获取指定页"""
    pages = liteparse_data.get("pages", [])
    for p in pages:
        if p.get("page_number") == page_num:
            return p
    return None


def run_cross_validation(
    pdf_path: str,
    processed_results: dict,
    progress_callback=None,
) -> CrossValidateReport:
    """执行完整的交叉验证流程

    Args:
        pdf_path: PDF 文件路径
        processed_results: pdf2docx 处理结果（data.json 中的 data 部分）
        progress_callback: 进度回调 (step: str, current: int, total: int)

    Returns:
        CrossValidateReport
    """
    all_tables = processed_results.get("tables", [])
    total_pages = processed_results.get("total_pages", 0)

    report = CrossValidateReport(
        pdf_path=pdf_path,
        total_pages=total_pages,
    )

    if not all_tables:
        report.error = "无表格数据"
        return report

    # ============== Step 1: 规则分类 ==============
    if progress_callback:
        progress_callback("classify", 0, len(all_tables))

    logger.info(f"开始规则分类: {len(all_tables)} 页")

    classify_results = classify_all(all_tables)
    report.classify_results = classify_results

    real_tables = [r for r in classify_results if r.is_real_table]
    fake_tables = [r for r in classify_results if not r.is_real_table]

    logger.info(
        f"分类完成: 真表格 {len(real_tables)}, 假表格 {len(fake_tables)}"
    )

    if progress_callback:
        progress_callback("classify_done", len(all_tables), len(all_tables))

    if not real_tables:
        logger.info("无真表格页，跳过 LLM 验证")
        return report

    # ============== Step 2: 加载 liteparse 数据 ==============
    if progress_callback:
        progress_callback("load_liteparse", 0, len(real_tables))

    liteparse_data = _load_liteparse_pages(pdf_path)

    if liteparse_data is None:
        report.error = "liteparse 缓存数据不可用——请确保 PDF 已完成解析（liteparse 侧通道已运行）"
        logger.warning(report.error)
        return report

    # ============== Step 3: LLM 逐页验证 ==============
    llm_results: List[LLMVerifyResult] = []

    for idx, cr in enumerate(real_tables):
        if progress_callback:
            progress_callback("verify", idx + 1, len(real_tables))

        page_num = cr.page
        table_entry = all_tables[cr.page - 1] if cr.page <= len(all_tables) else None
        # more robust: find table by page
        table_entry = None
        for t in all_tables:
            if t.get("page") == page_num:
                table_entry = t
                break

        if not table_entry:
            logger.warning(f"找不到第 {page_num} 页的表格数据")
            llm_results.append(LLMVerifyResult(
                page=page_num,
                llm_error=f"找不到第 {page_num} 页的表格数据",
            ))
            continue

        table_data = table_entry.get("data", [])
        if not table_data:
            llm_results.append(LLMVerifyResult(
                page=page_num,
                llm_error="表格 data 为空",
            ))
            continue

        # 获取 liteparse 对应页数据
        lp_page = _get_liteparse_page(liteparse_data, page_num)
        if lp_page is None:
            llm_results.append(LLMVerifyResult(
                page=page_num,
                llm_error="liteparse 未解析此页",
            ))
            continue

        full_text = lp_page.get("full_text", "")
        table_regions = lp_page.get("table_regions", [])

        if not full_text.strip():
            llm_results.append(LLMVerifyResult(
                page=page_num,
                llm_error="liteparse full_text 为空",
            ))
            continue

        # 调用 LLM
        logger.info(f"LLM 验证第 {page_num} 页...")
        v_result = verify_table_with_llm(
            page_num=page_num,
            liteparse_full_text=full_text,
            table_data=table_data,
            table_regions=table_regions,
        )
        llm_results.append(v_result)

    report.llm_results = llm_results

    if progress_callback:
        progress_callback("done", len(real_tables), len(real_tables))

    logger.info(
        f"交叉验证完成: {report.real_table_count} 真表格, "
        f"{report.fake_table_count} 假表格, "
        f"{report.verified_count} 已验证"
    )

    return report


def quick_classify(processed_results: dict) -> List[ClassifyResult]:
    """仅运行规则分类（不调用 LLM），用于快速预览"""
    all_tables = processed_results.get("tables", [])
    return classify_all(all_tables)
