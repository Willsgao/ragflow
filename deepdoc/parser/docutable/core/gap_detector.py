# -*- coding: utf-8 -*-
"""
PDF表格提取器 - 列边界检测模块
"""

import statistics


def detect_column_boundaries_adaptive(text_blocks):
    """自适应检测列边界（基于x坐标分布）
    
    Args:
        text_blocks: 文本块列表，每个包含 x0, x1 坐标
    
    Returns:
        列边界列表 [x0, x1, x2, ...]
    """
    if not text_blocks:
        return []
    
    all_x0 = [b.get("x0", b.get("x0", 0)) for b in text_blocks]
    all_x1 = [b.get("x1", b.get("x1", 0)) for b in text_blocks]
    
    if not all_x0:
        return []
    
    min_x = min(all_x0)
    max_x = max(all_x1)
    
    x_points = sorted(set(all_x0 + all_x1))
    if len(x_points) < 2:
        return [min_x, max_x]
    
    # 计算自适应列边界阈值
    gaps = []
    for i in range(len(x_points) - 1):
        gap = x_points[i + 1] - x_points[i]
        gaps.append((x_points[i], x_points[i + 1], gap))
    
    all_gaps = [g[2] for g in gaps if g[2] > 0]
    if all_gaps:
        median_gap = statistics.median(all_gaps)
        gap_threshold = max(median_gap * 1.5, 10)
    else:
        gap_threshold = 15
    
    column_boundaries = []
    for x_start, x_end, gap in gaps:
        if gap > gap_threshold:
            column_boundaries.append((x_start + x_end) / 2)
    
    if not column_boundaries:
        return [min_x, max_x]
    
    column_boundaries = sorted(set(column_boundaries))
    if column_boundaries[0] > min_x:
        column_boundaries.insert(0, (min_x + column_boundaries[0]) / 2)
    if column_boundaries[-1] < max_x:
        column_boundaries.append((column_boundaries[-1] + max_x) / 2)
    
    return column_boundaries


class GapDetector:
    """基于间隙检测的列边界分析"""
    
    def __init__(self, bucket_size=15, gap_threshold=15):
        """
        Args:
            bucket_size: x坐标分桶大小
            gap_threshold: 间隙阈值（大于此值认为是列分隔）
        """
        self.bucket_size = bucket_size
        self.gap_threshold = gap_threshold
    
    def detect_columns(self, text_blocks):
        """检测列边界
        
        Args:
            text_blocks: 文本块列表
        
        Returns:
            列边界列表 [x0, x1, x2, ...]
        """
        # 使用自适应方法
        return detect_column_boundaries_adaptive(text_blocks)
    
    def detect_rows(self, text_blocks):
        """检测行边界"""
        if not text_blocks:
            return []
        
        all_y = sorted(set([b.get("y0", 0) for b in text_blocks]))
        
        if len(all_y) < 2:
            return all_y
        
        gaps = []
        for i in range(len(all_y) - 1):
            gap = all_y[i + 1] - all_y[i]
            if gap > 5:  # 间距大于5px认为是行间隙
                mid = (all_y[i] + all_y[i + 1]) / 2
                gaps.append(mid)
        
        return sorted(gaps)
