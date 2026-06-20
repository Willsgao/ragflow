# -*- coding: utf-8 -*-
"""
PDF表格提取器 - 列分析模块
"""

import numpy as np


class ColumnAnalyzer:
    """列分析器"""
    
    def __init__(self):
        self.min_col_width = 10  # 最小列宽
    
    def analyze(self, text_blocks):
        """分析文本块分布，返回列信息
        
        Returns:
            dict: {
                'column_count': 列数,
                'boundaries': [x0, x1, ...],
                'centers': [center_x, ...]
            }
        """
        if not text_blocks:
            return None
        
        x_coords = [b.get("x0", 0) for b in text_blocks]
        
        return {
            'column_count': 0,
            'boundaries': [],
            'centers': []
        }
    
    def find_optimal_k(self, x_coords):
        """使用轮廓系数找最优列数"""
        try:
            from sklearn.cluster import KMeans
            from sklearn.metrics import silhouette_score
            
            X = np.array(x_coords).reshape(-1, 1)
            
            best_k = 2
            best_score = -1
            
            for k in range(2, min(10, len(x_coords) // 3 + 1)):
                kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
                labels = kmeans.fit_predict(X)
                score = silhouette_score(X, labels)
                
                if score > best_score:
                    best_score = score
                    best_k = k
            
            return best_k
            
        except ImportError:
            return 4  # 默认4列
