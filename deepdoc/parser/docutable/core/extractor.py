# -*- coding: utf-8 -*-
"""
PDF表格提取器 - PDF解析核心模块

双引擎表格提取：pdfplumber（表格检测）+ PyMuPDF（文本块 + TableProcessor 重建）
"""

import logging
from pathlib import Path

import pdfplumber

from .table_processor import TableProcessor

logger = logging.getLogger(__name__)

# PyMuPDF (fitz) 在部分部署环境可能未安装，设为可选
try:
    import fitz
    _HAS_FITZ = True
except ImportError:
    fitz = None
    _HAS_FITZ = False


class PDFExtractor:
    """PDF 表格提取器。

    双引擎模式：
      - pdfplumber: 原生 extract_tables()，适合有明确表格线/结构的 PDF
      - PyMuPDF: 提取文本块 + TableProcessor 基于空间位置重建表格，
                 适合无边框表格或 pdfplumber 漏检的场景

    调用示例:
        ext = PDFExtractor()
        results = ext.extract("report.pdf")
        for r in results:
            print(r["page"], r["type"], len(r["data"]), "rows")
    """

    # 默认财务关键词（用于页面预筛，减少无效处理）
    FINANCIAL_KEYWORDS = [
        "万元", "元", "百万", "十亿", "%", "比率",
        "资产", "负债", "收入", "利润", "现金",
        "股东", "资本", "充足率", "率", "额", "数",
    ]

    def __init__(self, check_keywords=False):
        """初始化提取器。

        Args:
            check_keywords: 是否启用财务关键词预筛。
                            设为 False 则处理所有页面（适用非财务 PDF）。
        """
        self.check_keywords = check_keywords
        self.table_processor = TableProcessor()

    def extract(self, pdf_path, method="auto"):
        """提取 PDF 中的表格。

        Args:
            pdf_path: PDF 文件路径。
            method:   "auto" | "pdfplumber" | "pymupdf"。

        Returns:
            list[dict]: 每个 dict 包含:
                page, type("table"), data([[cell, ...], ...]),
                text(页面全文), extractor, bbox(可选)
        """
        if method == "auto":
            return self._extract_auto(pdf_path)
        if method == "pymupdf":
            return self._extract_pymupdf(pdf_path)
        if method == "pdfplumber":
            return self._extract_pdfplumber(pdf_path)
        return []

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _extract_auto(self, pdf_path):
        """自动模式：pdfplumber 为主，PyMuPDF 补漏。

        pdfplumber 擅长检测有边框的表格，PyMuPDF + TableProcessor
        擅长从空间布局中重建无边框/松散的表格。
        当 pdfplumber 有结果时，PyMuPDF 结果作为补充；
        当 pdfplumber 无结果时，PyMuPDF 兜底。
        """
        results_pp = []
        results_mu = []

        try:
            results_pp = self._extract_pdfplumber(pdf_path)
        except Exception:
            logger.debug("pdfplumber extract failed", exc_info=True)

        try:
            results_mu = self._extract_pymupdf(pdf_path)
        except Exception:
            logger.debug("PyMuPDF extract failed", exc_info=True)

        # 合并：pdfplumber 优先，PyMuPDF 补充不重叠的页
        pp_pages = {r["page"] for r in results_pp}
        merged = list(results_pp)
        for r in results_mu:
            if r["page"] not in pp_pages:
                merged.append(r)

        return merged

    def _extract_pdfplumber(self, pdf_path):
        """pdfplumber 原生表格提取。"""
        results = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                tables = page.find_tables()
                words = page.extract_words()

                if not tables and not words:
                    continue

                full_text = " ".join(w.get("text", "") for w in words)

                if self.check_keywords and not self._has_keyword_match(full_text):
                    continue

                for table in tables:
                    table_data = table.extract()
                    if not table_data or len(table_data) <= 1:
                        continue

                    cleaned = self._clean_table(table_data)
                    if not cleaned or len(cleaned) <= 1:
                        continue

                    bbox = list(table.bbox) if table.bbox else None
                    results.append({
                        "page": page_num + 1,
                        "type": "table",
                        "data": cleaned,
                        "text": full_text,
                        "extractor": "pdfplumber",
                        "bbox": bbox,  # [x0, y0, x1, y1] or None
                    })

        return results

    def _extract_pymupdf(self, pdf_path):
        """PyMuPDF 文本块提取 + TableProcessor 表格重建。

        对每一页：
        1. 用 PyMuPDF 按 span 粒度提取所有文本块（带 bbox）
        2. 用 TableProcessor.reconstruct_table() 尝试重建为 2D 表格
        3. 重建成功则返回 "table" 类型，否则返回 "text" 类型（原始文本块）
        """
        if not _HAS_FITZ:
            logger.debug("PyMuPDF (fitz) not installed, skipping pymupdf extraction")
            return []
        results = []
        doc = fitz.open(pdf_path)

        for page_num, page in enumerate(doc):
            text_blocks = self._extract_text_blocks_from_page(page)
            if not text_blocks:
                continue

            full_text = " ".join(b["text"] for b in text_blocks)

            if self.check_keywords and not self._has_keyword_match(full_text):
                continue

            # ---- 核心改动：调用 TableProcessor 重建表格 ----
            page_rect = page.rect
            reconstructed = self.table_processor.reconstruct_table(
                text_blocks,
                page_rect=[page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1],
            )

            if reconstructed and len(reconstructed) > 1:
                # 规范化列数
                normalized = self.table_processor.normalize_columns(reconstructed)
                # 计算 bbox（用所有文本块的外接矩形）
                bbox = self._compute_blocks_bbox(text_blocks)
                results.append({
                    "page": page_num + 1,
                    "type": "table",
                    "data": normalized,
                    "text": full_text,
                    "extractor": "pymupdf+processor",
                    "bbox": bbox,
                })
            else:
                # 重建失败，仍返回原始文本块供参考
                bbox = self._compute_blocks_bbox(text_blocks)
                results.append({
                    "page": page_num + 1,
                    "type": "text",
                    "data": [[b["text"] for b in text_blocks]],  # 包装为 2D
                    "text": full_text,
                    "extractor": "pymupdf",
                    "bbox": bbox,
                })

        doc.close()
        return results

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_blocks_from_page(page):
        """从 PyMuPDF Page 中提取所有文本块（span 粒度）。"""
        blocks = []
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text:
                        b = span.get("bbox", [0, 0, 0, 0])
                        blocks.append({
                            "text": text,
                            "x0": b[0],
                            "y0": b[1],
                            "x1": b[2],
                            "y1": b[3],
                        })
        return blocks

    @staticmethod
    def _compute_blocks_bbox(text_blocks):
        """计算文本块列表的外接矩形。"""
        if not text_blocks:
            return None
        x0 = min(b["x0"] for b in text_blocks)
        y0 = min(b["y0"] for b in text_blocks)
        x1 = max(b["x1"] for b in text_blocks)
        y1 = max(b["y1"] for b in text_blocks)
        return [x0, y0, x1, y1]

    def _has_keyword_match(self, text):
        """检查文本是否包含财务关键词且长度足够。"""
        return any(kw in text for kw in self.FINANCIAL_KEYWORDS) and len(text) > 50

    def _clean_table(self, table_raw):
        """清理 pdfplumber 提取的原始表格：去除空行和空列。"""
        if not table_raw:
            return None

        # 过滤空行
        cleaned_rows = []
        for row in table_raw:
            if not row:
                continue
            row_clean = [cell.strip() if cell else "" for cell in row]
            if any(cell for cell in row_clean):
                cleaned_rows.append(row_clean)

        if not cleaned_rows:
            return None

        # 过滤空列
        num_cols = len(cleaned_rows[0])
        non_empty_cols = []
        for col_idx in range(num_cols):
            col_values = [
                row[col_idx] if col_idx < len(row) else ""
                for row in cleaned_rows
            ]
            if any(val.strip() for val in col_values if val):
                non_empty_cols.append(col_idx)

        final_rows = []
        for row in cleaned_rows:
            new_row = [row[i] if i < len(row) else "" for i in non_empty_cols]
            final_rows.append(new_row)

        return final_rows if final_rows else None

    def convert_to_images(self, pdf_path, output_dir=None, dpi=150):
        """将 PDF 转换为图片（需要 PyMuPDF）。"""
        if not _HAS_FITZ:
            logger.warning("PyMuPDF (fitz) not installed, convert_to_images unavailable")
            return []
        if output_dir is None:
            output_dir = Path(pdf_path).parent / "images"

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        doc = fitz.open(pdf_path)
        image_paths = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            output_path = output_dir / f"page_{page_num + 1}.png"
            pix.save(str(output_path))
            image_paths.append(str(output_path))

        doc.close()
        return image_paths
