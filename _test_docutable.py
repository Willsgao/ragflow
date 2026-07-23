import sys
sys.path.insert(0, 'deepdoc/parser')
from docutable.core import PDFExtractor
import os

pdf = r'f:\wills\codes\ragflow\test\benchmark\test_docs\Doc1.pdf'
ext = PDFExtractor()
results = ext.extract(pdf, method='auto')

print(f'提取到 {len(results)} 个结果')
for r in results:
    print(f'  第{r["page"]}页 [{r["type"]}]({r["extractor"]})')
    data = r.get('data', '')
    if isinstance(data, list) and data:
        for i, row in enumerate(data):
            if i >= 3:
                print(f'    ... 共 {len(data)} 行')
                break
            print('    | ' + ' | '.join(str(c)[:25] for c in row))
