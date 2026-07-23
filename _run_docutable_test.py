from deepdoc.parser.docutable.core import PDFExtractor
ext = PDFExtractor()
results = ext.extract('/tmp/test.pdf', method='auto')
print(f'提取到 {len(results)} 个表格')
for r in results:
    print(f'  第{r["page"]}页 [{r["type"]}]({r["extractor"]})')
    data = r.get('data', [])
    if data:
        for i, row in enumerate(data[:3]):
            print('    | ' + ' | '.join(str(c)[:20] for c in row))
        if len(data) > 3:
            print(f'    ... 共 {len(data)} 行')
