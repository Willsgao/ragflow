import logging, sys, os
from common.log_utils import init_root_logger
init_root_logger('docutable_test')

sys.path.insert(0, 'deepdoc/parser')
from deepdoc.parser.pdf_parser import DocuTableExtractor, DocuTableProcessor, DocuTableExporter

available = DocuTableExtractor is not None
enabled = os.getenv('ENABLE_DOCUTABLE', '')
print('DocuTableExtractor available: ' + str(available))
print('ENABLE_DOCUTABLE: ' + enabled)

ext = DocuTableExtractor()
results = ext.extract(r'f:\wills\codes\ragflow\test\benchmark\test_docs\Doc1.pdf')
print('docutable extracted ' + str(len(results)) + ' results')
print('Check logs/docutable_test.log for log output')
