# -*- coding: utf-8 -*-
"""
PDF表格提取器 - 表格处理模块
负责根据文本位置重建表格结构
"""

from .gap_detector import GapDetector
from .column_analyzer import ColumnAnalyzer


class TableProcessor:
    """表格处理核心类"""
    
    def __init__(self):
        self.gap_detector = GapDetector()
        self.column_analyzer = ColumnAnalyzer()
    
    def reconstruct_table(self, text_blocks, page_rect=None, method='auto'):
        """重建表格结构
        
        Args:
            text_blocks: 文本块列表，每个包含 text, x0, y0, x1, y1
            page_rect: 页面边界矩形
            method: 重建方法 ('gap', 'clustering', 'header', 'auto')
        
        Returns:
            表格数据列表
        """
        if not text_blocks:
            return None
        
        if method == 'auto':
            # 尝试多种方法
            result = self._try_all_methods(text_blocks, page_rect)
        elif method == 'gap':
            result = self._reconstruct_by_gap(text_blocks, page_rect)
        elif method == 'clustering':
            result = self._reconstruct_by_clustering(text_blocks, page_rect)
        elif method == 'header':
            result = self._reconstruct_by_header(text_blocks, page_rect)
        else:
            result = None
        
        return result
    
    def _try_all_methods(self, text_blocks, page_rect):
        """尝试所有方法，返回第一个成功的"""
        methods = [
            self._reconstruct_by_gap,
            self._reconstruct_by_clustering,
        ]
        
        for method in methods:
            result = method(text_blocks, page_rect)
            if result and len(result) > 1:
                return result
        
        return None
    
    def _reconstruct_by_gap(self, text_blocks, page_rect):
        """基于间隙检测的表格重建"""
        # 检测列边界
        column_boundaries = self.gap_detector.detect_columns(text_blocks)
        
        if not column_boundaries or len(column_boundaries) <= 2:
            return None
        
        # 按行分组
        rows = self._group_by_rows(text_blocks)
        
        # 构建表格
        table_data = []
        for row_blocks in rows:
            row_blocks.sort(key=lambda b: b.get("x0", 0))
            row_data = [""] * (len(column_boundaries) - 1)
            
            for block in row_blocks:
                col_idx = self._find_column_index(block, column_boundaries)
                if 0 <= col_idx < len(row_data):
                    if row_data[col_idx]:
                        row_data[col_idx] += " " + block.get("text", "")
                    else:
                        row_data[col_idx] = block.get("text", "")
            
            if any(cell.strip() for cell in row_data):
                table_data.append(row_data)
        
        return table_data if len(table_data) > 1 else None
    
    def _reconstruct_by_clustering(self, text_blocks, page_rect):
        """基于聚类的表格重建"""
        try:
            import numpy as np
            from sklearn.cluster import KMeans
            
            # 收集x坐标
            x_coords = [b.get("x0", 0) for b in text_blocks if 20 < b.get("x0", 0) < 600]
            
            if len(x_coords) < 4:
                return None
            
            X = np.array(x_coords).reshape(-1, 1)
            
            # 尝试不同k值
            best_k = 2
            best_score = -1
            
            for k in range(2, min(10, len(x_coords) // 3 + 1)):
                kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
                labels = kmeans.fit_predict(X)
                
                try:
                    from sklearn.metrics import silhouette_score
                    score = silhouette_score(X, labels)
                    if score > best_score:
                        best_score = score
                        best_k = k
                except:
                    pass
            
            # 使用最优k
            kmeans = KMeans(n_clusters=best_k, random_state=42, n_init=10)
            kmeans.fit(X)
            centers = sorted(kmeans.cluster_centers_.flatten())
            
            # 构建列边界
            column_boundaries = [0]
            for i in range(len(centers) - 1):
                column_boundaries.append((centers[i] + centers[i + 1]) / 2)
            column_boundaries.append(700)
            
            # 按行分组
            rows = self._group_by_rows(text_blocks)
            
            # 构建表格
            table_data = []
            for row_blocks in rows:
                row_blocks.sort(key=lambda b: b.get("x0", 0))
                row_data = [""] * best_k
                
                for block in row_blocks:
                    x0 = block.get("x0", 0)
                    for i in range(len(column_boundaries) - 1):
                        if column_boundaries[i] <= x0 < column_boundaries[i + 1]:
                            if row_data[i]:
                                row_data[i] += " " + block.get("text", "")
                            else:
                                row_data[i] = block.get("text", "")
                            break
                
                if any(cell.strip() for cell in row_data):
                    table_data.append(row_data)
            
            return table_data if len(table_data) > 1 else None
            
        except ImportError:
            # 没有sklearn，回退到间隙检测
            return self._reconstruct_by_gap(text_blocks, page_rect)
    
    def _reconstruct_by_header(self, text_blocks, page_rect):
        """基于表头的表格重建"""
        # TODO: 实现基于表头的检测
        return self._reconstruct_by_gap(text_blocks, page_rect)
    
    def _group_by_rows(self, text_blocks):
        """按行分组"""
        if not text_blocks:
            return []
        
        # 按y坐标排序
        sorted_blocks = sorted(text_blocks, key=lambda b: b.get("y0", 0))
        
        rows = []
        current_row = []
        current_y = None
        y_threshold = 5
        
        for block in sorted_blocks:
            y = round(block.get("y0", 0) / y_threshold)
            
            if current_y is None or abs(y - current_y) <= 1:
                current_row.append(block)
                current_y = y
            else:
                if current_row:
                    rows.append(current_row)
                current_row = [block]
                current_y = y
        
        if current_row:
            rows.append(current_row)
        
        return rows
    
    def _find_column_index(self, block, column_boundaries):
        """找到文本块属于哪一列"""
        x0 = block.get("x0", 0)
        
        for i in range(len(column_boundaries) - 1):
            if column_boundaries[i] <= x0 < column_boundaries[i + 1]:
                return i
        
        return 0
    
    def normalize_columns(self, table_data):
        """规范化表格列数"""
        if not table_data:
            return table_data
        
        # 计算最大列数
        max_cols = max((len(row) for row in table_data if row), default=0)
        
        if max_cols == 0:
            return table_data
        
        def is_empty_row(row):
            if not row:
                return True
            return all(cell is None or str(cell).strip() == "" for cell in row)
        
        # 补全每行
        normalized = []
        for row in table_data:
            if not row:
                row = []
            while len(row) < max_cols:
                row.append(None)
            row = row[:max_cols]
            normalized.append(row)
        
        # 去除首尾空行
        start_idx = 0
        while start_idx < len(normalized) and is_empty_row(normalized[start_idx]):
            start_idx += 1
        
        end_idx = len(normalized)
        while end_idx > start_idx and is_empty_row(normalized[end_idx - 1]):
            end_idx -= 1
        
        return normalized[start_idx:end_idx]
