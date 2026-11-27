"""Run recognition and artifact rendering in a bounded worker process."""
import json
from pathlib import Path
import sys

from .office_scan import recognize
from .scan_word import build_scan_word, open_source, render_source_page
from .scan_layout import validate_scan_word


def main(folder):
    source = (folder / 'input.pdf').read_bytes()
    selection = folder / 'pages.json'
    pages = json.loads(selection.read_text()) if selection.is_file() else None
    result = recognize(source, pages, None, False, correct=True)
    word = build_scan_word(source, result)
    result['document_quality'] = validate_scan_word(word, result)
    (folder / 'result.docx').write_bytes(word)
    with open_source(source) as pdf:
        for page in result['pages']:
            number = page['page']
            with render_source_page(pdf, number, 120) as image:
                image.save(folder / f'page-{number}.png')
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main(Path(sys.argv[1]))
