# -*- coding: utf-8 -*-
"""
PDF表格提取器 - PDF解析核心模块
"""

import pdfplumber
import fitz  # PyMuPDF
from pathlib import Path


class PDFExtractor:
    """PDF解析器"""
    
    def __init__(self):
        self.financial_keywords = [
            "万元", "元", "百万", "十亿", "%", "比率",
            "资产", "负债", "收入", "利润", "现金",
            "股东", "资本", "充足率", "率", "额", "数"
        ]
    
    def extract(self, pdf_path, method='auto'):
        """提取PDF中的表格
        
        Args:
            pdf_path: PDF文件路径
            method: 提取方法 ('pymupdf', 'pdfplumber', 'auto')
        
        Returns:
            提取结果列表
        """
        if method == 'auto':
            return self._extract_auto(pdf_path)
        elif method == 'pymupdf':
            return self._extract_pymupdf(pdf_path)
        elif method == 'pdfplumber':
            return self._extract_pdfplumber(pdf_path)
        else:
            return []
    
    def _extract_auto(self, pdf_path):
        """自动选择最佳方法"""
        results = []
        
        # 方法1: pdfplumber（通常更准确）
        try:
            results = self._extract_pdfplumber(pdf_path)
        except Exception as e:
            print(f"pdfplumber提取失败: {e}")
        
        # 方法2: PyMuPDF（备选）
        if not results:
            try:
                results = self._extract_pymupdf(pdf_path)
            except Exception as e:
                print(f"PyMuPDF提取失败: {e}")
        
        return results
    
    def _extract_pdfplumber(self, pdf_path):
        """使用pdfplumber提取"""
        results = []
        
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                tables_data = page.extract_tables()
                words = page.extract_words()
                
                if not tables_data or not words:
                    continue
                
                full_text = " ".join([w.get("text", "") for w in words])
                
                if not self._is_financial_content(full_text):
                    continue
                
                # 清理并添加表格
                for table_raw in tables_data:
                    if table_raw and len(table_raw) > 1:
                        table_cleaned = self._clean_table(table_raw)
                        if table_cleaned and len(table_cleaned) > 1:
                            results.append({
                                "page": page_num + 1,
                                "type": "table",
                                "data": table_cleaned,
                                "text": full_text,
                                "extractor": "pdfplumber"
                            })
                            break  # 只取第一个表格
        
        return results
    
    def _extract_pymupdf(self, pdf_path):
        """使用PyMuPDF提取"""
        results = []
        
        doc = fitz.open(pdf_path)
        
        for page_num, page in enumerate(doc):
            page_rect = page.rect
            
            text_dict = page.get_text("dict")
            blocks = text_dict.get("blocks", [])
            
            text_blocks = []
            for block in blocks:
                if block.get("type") == 0:
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text = span.get("text", "").strip()
                            if text:
                                bbox = span.get("bbox", [0, 0, 0, 0])
                                text_blocks.append({
                                    "text": text,
                                    "x0": bbox[0],
                                    "y0": bbox[1],
                                    "x1": bbox[2],
                                    "y1": bbox[3],
                                })
            
            if not text_blocks:
                continue
            
            full_text = " ".join([b["text"] for b in text_blocks])
            
            if not self._is_financial_content(full_text):
                continue
            
            # TODO: 使用table_processor重建表格
            # 目前只返回原始文本
            results.append({
                "page": page_num + 1,
                "type": "text",
                "data": text_blocks,
                "text": full_text,
                "extractor": "pymupdf"
            })
        
        doc.close()
        return results
    
    def _is_financial_content(self, text):
        """检查是否包含财务关键字"""
        return any(kw in text for kw in self.financial_keywords) and len(text) > 50
    
    def _clean_table(self, table_raw):
        """清理pdfplumber提取的原始表格"""
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
            col_values = [row[col_idx] if col_idx < len(row) else "" for row in cleaned_rows]
            if any(val.strip() for val in col_values if val):
                non_empty_cols.append(col_idx)
        
        final_rows = []
        for row in cleaned_rows:
            new_row = [row[i] if i < len(row) else "" for i in non_empty_cols]
            final_rows.append(new_row)
        
        return final_rows if final_rows else None
    
    def convert_to_images(self, pdf_path, output_dir=None, dpi=150):
        """将PDF转换为图片"""
        if output_dir is None:
            output_dir = Path(pdf_path).parent / "images"
        
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        doc = fitz.open(pdf_path)
        image_paths = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            mat = fitz.Matrix(dpi/72, dpi/72)
            pix = page.get_pixmap(matrix=mat)
            
            output_path = output_dir / f"page_{page_num + 1}.png"
            pix.save(str(output_path))
            image_paths.append(str(output_path))
        
        doc.close()
        return image_paths
