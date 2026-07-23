import sys
sys.path.insert(0, 'deepdoc/parser')
from docutable.core import PDFExtractor
import pdfplumber

pdf = r'f:\wills\codes\ragflow\test\benchmark\test_docs\Doc1.pdf'
ext = PDFExtractor()

# 直接调用底层方法，绕过关键词过滤
print('=== pdfplumber 底层提取 ===')
results = ext._extract_pdfplumber(pdf)
print(f'提取到 {len(results)} 个结果')

# 用 pdfplumber 直接看表格
print()
print('=== 检查三个测试PDF中哪个有表格 ===')
import os
for name in ['Doc1.pdf', 'Doc2.pdf', 'Doc3.pdf']:
    fp = os.path.join(r'f:\wills\codes\ragflow\test\benchmark\test_docs', name)
    with pdfplumber.open(fp) as doc:
        total_tables = sum(1 for p in doc.pages for t in (p.extract_tables() or []) if t)
        text = ' '.join(w.get('text','') for p in doc.pages for w in (p.extract_words() or []))
        has_kw = any(kw in text for kw in ['万元','资产','负债','收入','%','元','金额','合计'])
        print(f'{name}: {len(doc.pages)}页, {total_tables}个表格, 财务关键词={has_kw}')
