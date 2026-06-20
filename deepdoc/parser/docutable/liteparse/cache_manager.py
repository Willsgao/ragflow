# -*- coding: utf-8 -*-
"""
LiteParse Extractor — 缓存管理器

与现有 data/mid_cache/<pdf_name>/ 体系对齐：
中间数据存放在 data/mid_cache/<pdf_name>/liteparse/ 子目录下。

缓存结构：
  liteparse/
  ├── metadata.json      # 解析元数据（PDF 信息、时间、状态）
  ├── pages.json         # 所有页面完整数据（ParseResult）
  └── pages/             # 逐页文本（方便人工查看和差分对比）
      ├── page_001.txt
      ├── page_002.txt
      └── ...
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from .config import MID_CACHE_ROOT, CACHE_SUBDIR
from .models import ParseResult

# ============================================================
# 文件哈希（与现有 utils.py 对齐）
# ============================================================

def _compute_pdf_hash(pdf_path: str) -> str:
    """计算 PDF 文件哈希（首尾各 1MB 的 MD5）。

    与 codes/pdf_extractor/utils.py 中的校验机制一致。
    """
    file_size = os.path.getsize(pdf_path)
    md5 = hashlib.md5()
    chunk_size = 1024 * 1024  # 1MB
    with open(pdf_path, "rb") as f:
        # 首 1MB
        head = f.read(chunk_size)
        md5.update(head)
        if file_size > chunk_size * 2:
            # 尾 1MB
            f.seek(-chunk_size, os.SEEK_END)
            tail = f.read(chunk_size)
            md5.update(tail)
        elif file_size > chunk_size:
            # 文件不足 2MB，剩余部分
            rest = f.read()
            md5.update(rest)
    return md5.hexdigest()


# ============================================================
# 缓存目录
# ============================================================

def _sanitize_pdf_name(pdf_name: str, pdf_path: str = "") -> str:
    """清理 PDF 文件名以用作目录名，与 pdf_extractor/utils.py 的 get_pdf_cache_dir 保持一致。

    处理全角/半角冒号、Windows 非法字符、乱码等问题。
    """
    pdf_name = pdf_name[:100]
    # 移除 Windows 文件名非法字符：\\ / : * ? \" < > |
    for ch in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
        pdf_name = pdf_name.replace(ch, '_')
    # strip 首尾空格和点号
    pdf_name = pdf_name.strip().rstrip('.')
    # 仅保留中文、英文、数字、下划线、连字符
    sanitized = ''.join(
        c if c == '_' or c == '-' or c.isalnum() or '\u4e00' <= c <= '\u9fff' else '_'
        for c in pdf_name
    )
    # 如果 sanitize 后为空，用文件 hash 兜底
    if not sanitized.strip('_') and pdf_path:
        file_hash = _compute_pdf_hash(pdf_path)
        sanitized = file_hash[:16]
    return sanitized


def _get_cache_dir(pdf_path: str) -> Path:
    """获取 liteparse 专属缓存目录。

    data/mid_cache/<sanitized_pdf_name>/liteparse/
    文件名清理与主缓存路径完全一致，兼容含特殊字符（如全角冒号）的PDF文件名。
    """
    pdf_name = Path(pdf_path).stem
    sanitized = _sanitize_pdf_name(pdf_name, pdf_path)
    return MID_CACHE_ROOT / sanitized / CACHE_SUBDIR


def _ensure_cache_dir(pdf_path: str) -> Path:
    """确保缓存目录存在并返回路径。"""
    cache_dir = _get_cache_dir(pdf_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


# ============================================================
# 保存
# ============================================================

def save_parse_result(
    result: ParseResult,
    save_per_page_txt: bool = True,
) -> Path:
    """将 ParseResult 保存到中间缓存目录。

    Args:
        result: 解析结果
        save_per_page_txt: 是否额外输出逐页 txt 文件

    Returns:
        缓存目录路径
    """
    cache_dir = _ensure_cache_dir(result.pdf_path)

    # 1. 保存 metadata.json
    file_size = 0
    file_mtime = 0.0
    if os.path.isfile(result.pdf_path):
        file_size = os.path.getsize(result.pdf_path)
        file_mtime = os.path.getmtime(result.pdf_path)

    metadata = {
        "pdf_path": result.pdf_path,
        "file_hash": _compute_pdf_hash(result.pdf_path)
        if os.path.isfile(result.pdf_path) else "",
        "file_size": file_size,
        "file_modified_time": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(file_mtime)
        ),
        "total_pages": result.total_pages,
        "parse_time_sec": round(result.parse_time_sec, 2),
        "table_page_count": result.page_count_with_table,
        "cached_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": result.error,
    }
    _write_json(cache_dir / "metadata.json", metadata)

    # 2. 保存 pages.json（完整数据）
    pages_data = result.to_dict()
    _write_json(cache_dir / "pages.json", pages_data)

    # 3. 逐页 txt
    if save_per_page_txt:
        pages_dir = cache_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        for page in result.pages:
            txt_path = pages_dir / f"page_{page.page_number:03d}.txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"# Page {page.page_number}\n")
                f.write(f"# Size: {page.page_width:.1f} x {page.page_height:.1f}\n")
                f.write(f"# Is table page: {page.is_table_page}\n")
                if page.table_regions:
                    f.write(f"# Table regions: {len(page.table_regions)}\n")
                    for i, tr in enumerate(page.table_regions):
                        f.write(
                            f"#   Region {i}: ({tr.x0:.0f},{tr.y0:.0f})-"
                            f"({tr.x1:.0f},{tr.y1:.0f}) "
                            f"conf={tr.confidence:.2f}\n"
                        )
                f.write("\n")
                f.write(page.full_text)
                if page.table_regions:
                    f.write("\n\n# --- Table Region Texts ---\n")
                    for i, tr in enumerate(page.table_regions):
                        f.write(f"\n## Region {i}\n")
                        f.write(tr.region_text)

    return cache_dir


# ============================================================
# 加载
# ============================================================

def load_parse_result(pdf_path: str) -> Optional[ParseResult]:
    """从缓存加载解析结果。

    校验 PDF 文件哈希，不匹配则返回 None（暗示缓存失效）。
    """
    cache_dir = _get_cache_dir(pdf_path)
    pages_json = cache_dir / "pages.json"

    if not pages_json.exists():
        return None

    data = _read_json(pages_json)
    if data is None:
        return None

    result = ParseResult.from_dict(data)

    # 校验文件哈希
    if os.path.isfile(pdf_path):
        cached_hash = _read_json(cache_dir / "metadata.json")
        if cached_hash:
            expected_hash = cached_hash.get("file_hash", "")
            actual_hash = _compute_pdf_hash(pdf_path)
            if expected_hash and actual_hash != expected_hash:
                return None  # 缓存失效

    return result


def is_cache_valid(pdf_path: str) -> bool:
    """快速检查缓存是否有效（不加载全量数据）。"""
    cache_dir = _get_cache_dir(pdf_path)
    metadata_json = cache_dir / "metadata.json"
    if not metadata_json.exists():
        return False
    if not (cache_dir / "pages.json").exists():
        return False
    if not os.path.isfile(pdf_path):
        return False
    meta = _read_json(metadata_json)
    if not meta:
        return False
    expected_hash = meta.get("file_hash", "")
    if expected_hash:
        actual_hash = _compute_pdf_hash(pdf_path)
        return actual_hash == expected_hash
    return True


def get_cache_dir_path(pdf_path: str) -> Path:
    """获取缓存目录路径（不创建）。"""
    return _get_cache_dir(pdf_path)


def delete_cache(pdf_path: str) -> bool:
    """删除 liteparse 缓存目录。"""
    import shutil
    cache_dir = _get_cache_dir(pdf_path)
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
        return True
    return False


# ============================================================
# 工具
# ============================================================

def _write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
