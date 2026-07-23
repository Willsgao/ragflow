from deepdoc.parser.docutable.core import PDFExtractor
ext = PDFExtractor()
results = ext.extract('/tmp/test.pdf', method='auto')
print(f'extracted {len(results)} tables')
for r in results:
    print(f'  page {r["page"]} [{r["type"]}]({r["extractor"]})')
    data = r.get('data', [])
    if data:
        for i, row in enumerate(data[:3]):
            print('    | ' + ' | '.join(str(c)[:20] for c in row))
        if len(data) > 3:
            print(f'    ... {len(data)} rows total')
