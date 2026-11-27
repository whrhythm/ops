"""Local scan recognition with review markers in original-page coordinates."""
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import re
from time import perf_counter

from .scan_geometry import deskew, ruled_table, source_region


NUMERIC = re.compile(r'[+−-]?\d+(?:[.,．]\d+)*[%％]?')


def restore_line_spaces(content, matched):
    lines = [text.strip() for _, text, _ in matched if text.strip()]
    if len(lines) < 2 or ''.join(''.join(line.split()) for line in lines) != ''.join(content.split()):
        return content
    # Keep source word spaces where a wrapped OCR line runs into another word.
    joined = lines[0]
    for line in lines[1:]:
        if re.search(r'[A-Za-z0-9.,:;!?)]$', joined) and re.match(r'[A-Za-z0-9]', line):
            joined += ' '
        joined += line
    return joined


def recognize_formula_regions(image, box, formula):
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    left, top, right, bottom = map(int, box)
    left, top = max(0, left - 4), max(0, top - 4)
    right, bottom = min(width, right + 4), min(height, bottom + 4)
    crop = image[top:bottom, left:right]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = np.flatnonzero(np.count_nonzero(gray < 175, axis=1) > 2)
    # A wide blank band can separate independent equations. A fraction bar or
    # matrix bracket connects its rows, so keep those regions together.
    breaks = np.flatnonzero(np.diff(ink) > max(10, len(ink) * .24)) + 1
    groups = np.split(ink, breaks) if 0 < len(breaks) < 8 else []
    if not groups or any(len(group) < 5 for group in groups):
        regions = [(top, bottom)]
    else:
        regions = [(max(top, top + int(group[0]) - 7),
                    min(bottom, top + int(group[-1]) + 8)) for group in groups]
    recognized = []
    for start, end in regions:
        predicted, = formula.predict(image[start:end, left:right].copy())
        recognized.append((predicted.json['res']['rec_formula'], (left, start, right, end)))
    return recognized


class Table(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows = []
        self.cell = None
        self.spans = False
        self.header_rows = []
        self.in_head = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == 'thead':
            self.in_head = True
        if tag == 'tr':
            self.rows.append([])
            if self.in_head:
                self.header_rows.append(len(self.rows)-1)
        if tag in {'td', 'th'} and self.rows:
            if tag == 'th' and len(self.rows)-1 not in self.header_rows:
                self.header_rows.append(len(self.rows)-1)
            self.cell = []
            self.spans |= any(key in {'rowspan', 'colspan'} and value != '1' for key, value in attrs)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'thead':
            self.in_head = False
        if tag in {'td', 'th'} and self.cell is not None:
            self.rows[-1].append(''.join(self.cell).strip())
            self.cell = None


def blocks_from_page(result, number, width, height, needs_review, image=None, inverse=None, formula=None):
    result = result.get('res', result)
    text_ocr = result['overall_ocr_res']
    lines = list(zip(text_ocr['rec_boxes'], text_ocr['rec_texts'], text_ocr['rec_scores'], strict=True))
    blocks = []
    for order, item in enumerate(result['parsing_res_list'], 1):
        left, top, right, bottom = item['block_bbox']
        label = item['block_label']
        content = item['block_content']
        matched = []
        for (x1, y1, x2, y2), text, score in lines:
            if left <= (x1 + x2) / 2 <= right and top <= (y1 + y2) / 2 <= bottom:
                matched.append(((x1, y1, x2, y2), text, score))
        reasons = ['degraded_scan'] if needs_review else []
        confidence = min((score for _, _, score in matched), default=None)
        if label not in {'image', 'chart', 'table', 'formula', 'display_formula', 'inline_formula'}:
            if confidence is None or confidence < 0.90:
                reasons.append('low_or_missing_line_confidence')
        block = {
            'block_id': f'p{number}-b{order}', 'label': label, 'order': order, 'page': number,
            **source_region(item['block_bbox'], width, height, inverse), 'minimum_line_confidence': confidence,
            'review_reasons': reasons,
        }
        if label == 'table':
            table = Table(content)
            block['rows'] = table.rows
            grid = ruled_table(image, item['block_bbox'], lines) if image is not None else None
            if grid is not None and not table.spans:
                block['rows'] = [[cell['text'] for cell in row] for row in grid]
                block['cell_order'] = 'visible_grid'
                block['cells'] = [[{'text': cell['text'], 'confidence': cell['confidence'],
                                    **source_region(cell['box'], width, height, inverse),
                                    'review_required': needs_review or cell['confidence'] is None
                                    or cell['confidence'] < .90 or bool(re.search(r'\d', cell['text']))}
                                   for cell in row] for row in grid]
            elif image is not None or table.spans or not table.rows:
                reasons.append('table_structure_needs_review')
            reasons.append('verify_key_cells_against_source')
            compact = lambda value: re.sub(r'\s+', '', value)
            block['header_rows'] = [i for i in table.header_rows if i < len(block['rows'])
                and list(map(compact, table.rows[i])) == list(map(compact, block['rows'][i]))]
        elif label not in {'image', 'chart'}:
            block['text'] = restore_line_spaces(content, matched) if label == 'text' else content
            if 'formula' in label:
                reasons.append('verify_formula_against_source')
                if formula is not None:
                    regions = recognize_formula_regions(image, item['block_bbox'], formula)
                    block['formula_lines'] = [expression for expression, _ in regions]
                    block['text'] = '\n'.join(block['formula_lines'])
                else:
                    regions = [(content, item['block_bbox'])]
                block['review_marks'] = [
                    {'kind': 'formula', 'text': expression, **source_region(box, width, height, inverse)}
                    for expression, box in regions
                ]
            elif NUMERIC.search(content):
                reasons.append('verify_key_values_against_source')
                block['review_marks'] = [
                    {'kind': 'number', 'text': value.group(), 'confidence': score,
                     **source_region(box, width, height, inverse)}
                    for box, text, score in matched for value in NUMERIC.finditer(text)
                ] or [{'kind': 'number', 'text': value.group(), 'bbox': block['bbox'], 'polygon': block['polygon']}
                      for value in NUMERIC.finditer(content)]
        blocks.append(block)
    return blocks


def scan_pipeline(formula_model='PP-FormulaNet_plus-L'):
    from paddleocr import FormulaRecognition, PPStructureV3

    options = {'device': 'cpu', 'cpu_threads': 4, 'enable_mkldnn': False}
    return (
        PPStructureV3(**options, use_doc_orientation_classify=False, use_doc_unwarping=False,
                      use_textline_orientation=False, use_formula_recognition=False,
                      use_chart_recognition=False, format_block_content=False,
                      text_detection_model_name='PP-OCRv5_mobile_det',
                      text_recognition_model_name='PP-OCRv5_server_rec'),
        FormulaRecognition(model_name=formula_model, **options),
    )


def simple_text_lines(page, number):
    """Use embedded text only when a page consists entirely of separated, horizontal text lines."""
    import pypdfium2.raw as pdfium

    if page.get_rotation() or any(obj.type != pdfium.FPDF_PAGEOBJ_TEXT for obj in page.get_objects()):
        return []
    left, bottom, right, top = page.get_bbox()
    width, height = right - left, top - bottom
    with closing(page.get_textpage()) as text_page:
        count = text_page.count_rects()
        if not 0 < count <= 250 or text_page.count_chars() > 16000:
            return []
        rects = sorted((text_page.get_rect(i) for i in range(count)), key=lambda rect: -rect[3])
        blocks = []
        previous_bottom = top
        for index, (x0, y0, x1, y1) in enumerate(rects, 1):
            if not (left <= x0 < x1 <= right and bottom <= y0 < y1 <= previous_bottom + 0.1):
                return []
            text = text_page.get_text_bounded(left=x0, bottom=y0, right=x1, top=y1).strip()
            if not text or '\ufffd' in text or '\n' in text:
                return []
            if y1 - y0 > (x1 - x0) * 2:
                return []
            region = source_region((x0-left, top-y1, x1-left, top-y0), width, height, None)
            blocks.append({'block_id': f'p{number}-b{index}', 'label': 'text', 'text': text,
                           'order': index, 'page': number, 'review_reasons': [], **region})
            previous_bottom = y0
    return blocks


def recognize(source, selected, pipeline, degraded, *, correct=False, formula=None):
    import cv2
    import numpy as np
    from .scan_word import open_source, render_source_page

    original = source if isinstance(source, bytes) else source.read_bytes()
    if not original or len(original) > 10 * 1024**2:
        raise ValueError('invalid scan input')
    source_hash = hashlib.sha256(original).hexdigest()
    pages = []
    with open_source(original) as pdf:
        page_count = len(pdf) if original.startswith(b'%PDF-') else 1
        selected = list(range(1, page_count+1) if selected is None else selected)
        if not selected or len(set(selected)) != len(selected) or not all(1 <= n <= page_count for n in selected):
            raise ValueError('page selection is out of range')
        for number in selected:
            started = perf_counter()
            if original.startswith(b'%PDF-'):
                with closing(pdf[number - 1]) as pdf_page:
                    native = simple_text_lines(pdf_page, number)
                    size = pdf_page.get_size()
                if native:
                    pages.append({'page': number, 'blocks': native, 'source_size': list(size),
                                  'method': 'embedded_text', 'review_required': False,
                                  'elapsed_s': round(perf_counter() - started, 3)})
                    continue
            with render_source_page(pdf, number, 200) as image:
                width, height = image.size
                raster = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            corrected, inverse, angle = deskew(raster) if correct else (raster, None, 0.0)
            if pipeline is None:
                pipeline, formula = scan_pipeline()
            raw, = pipeline.predict(corrected, use_table_orientation_classify=not correct)
            blocks = blocks_from_page(raw.json, number, width, height, degraded or abs(angle) >= 1,
                                      corrected if correct else None, inverse, formula)
            if not any(block.get('text', '').strip() or block.get('rows') for block in blocks):
                order = len(blocks) + 1
                blocks.append({'block_id': f'p{number}-b{order}', 'page': number, 'order': order,
                               'label': 'text', 'text': '', 'review_reasons': ['no_text_detected'],
                               **source_region((0, 0, width, height), width, height, None)})
            pages.append({'page': number, 'blocks': blocks, 'source_size': [width, height], 'method':'ocr',
                          'deskew_degrees': angle, 'corrected_to_source': inverse,
                          'elapsed_s': round(perf_counter() - started, 3),
                          'review_required': not blocks or any(b['review_reasons'] for b in blocks)})
    if not isinstance(source, bytes) and hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError('OCR source changed while processing')
    return {'schema_version': 1, 'editable_text_bookmarks': True, 'source_sha256': source_hash,
            'page_count': page_count, 'pages': pages}


