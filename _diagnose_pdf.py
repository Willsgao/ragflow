import sys
sys.path.insert(0, 'deepdoc/parser')
import pdfplumber

pdf = r'f:\wills\codes\ragflow\test\benchmark\test_docs\Doc1.pdf'

with pdfplumber.open(pdf) as doc:
    print(f'总页数: {len(doc.pages)}')
    for i, page in enumerate(doc.pages):
        text = page.extract_text() or ''
        tables = page.extract_tables() or []
        words = page.extract_words() or []
        word_text = ' '.join(w.get('text','') for w in words[:100])
        
        has_keywords = any(kw in word_text for kw in ['万元','资产','负债','收入','%','元'])
        
        print(f'--- 第{i+1}页 ---')
        print(f'  文字长度: {len(text)} 字符')
        print(f'  pdfplumber识别表格数: {len(tables)}')
        print(f'  含财务关键词: {has_keywords}')
        if word_text:
            print(f'  前200字预览: {word_text[:200]}')
