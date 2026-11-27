"""Bounded, deterministic layout for new OCR documents; never edits source files."""
from math import isfinite, sqrt
from statistics import median
from unicodedata import east_asian_width


def text_units(value):
    return sum(1 if east_asian_width(c) in ('W', 'F') else .55 for c in value)


def usable_width_twips(section):
    return round((section.page_width - section.left_margin - section.right_margin) / 635)


def column_widths(block, available_twips, font_points=10):
    rows = block['rows']
    count = max(map(len, rows))
    if not 1 <= count <= 30:
        raise ValueError('scan_table_column_count')
    contents = [[str(row[i]) for row in rows if i < len(row)] for i in range(count)]
    minimum = [round((max(2, min(4, max(map(text_units, values), default=0))) * font_points + 14) * 20)
               for values in contents]
    if sum(minimum) > available_twips:
        raise ValueError('scan_table_cannot_fit_readably')
    geometry = [[] for _ in range(count)]
    for row in block.get('cells') or []:
        if len(row) != count:
            continue
        previous = 0
        widths = []
        for cell in row:
            box = cell.get('bbox') if isinstance(cell, dict) else None
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(type(x) in (int, float) and isfinite(x) for x in box)
                    or not 0 <= box[0] < box[2] <= 1 or box[0] < previous - .003):
                break
            previous = box[2]
            widths.append(box[2] - box[0])
        if len(widths) == count:
            for i, width in enumerate(widths):
                geometry[i].append(width)
    if all(geometry):
        weights = [median(v) for v in geometry]
        method = 'source_cell_geometry'
    else:
        weights = [sqrt(max(map(text_units, values), default=0) + 1) for values in contents]
        method = 'text_length_fallback'
    # Water-fill: protect readable minimums without distorting all source proportions.
    remaining, active = available_twips, set(range(count))
    output = [0] * count
    while active:
        total = sum(weights[i] for i in active)
        small = [i for i in active if remaining * weights[i] / total < minimum[i]]
        if not small:
            ordered = sorted(active)
            for i in ordered[:-1]:
                output[i] = round(remaining * weights[i] / total)
            output[ordered[-1]] = remaining - sum(output[i] for i in ordered[:-1])
            break
        for i in small:
            output[i] = minimum[i]
            remaining -= minimum[i]
            active.remove(i)
    if any(a < b for a, b in zip(output, minimum)) or sum(output) != available_twips:
        raise ValueError('scan_table_width_allocation')
    return output, method


def configure_page(section, page):
    from docx.enum.section import WD_ORIENT
    from docx.shared import Mm
    size = page.get('source_size') or []
    landscape = len(size) == 2 and size[0] > size[1]
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (Mm(297), Mm(210)) if landscape else (Mm(210), Mm(297))
    section.left_margin = section.right_margin = Mm(15)
    section.top_margin = section.bottom_margin = Mm(14)
    return usable_width_twips(section)


def configure_table(table, block, section):
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, Twips
    available = usable_width_twips(section)
    widths, method = column_widths(block, available)
    table.autofit = False
    table._tbl.tblPr.find(qn('w:tblW')).set(qn('w:type'), 'dxa')
    table._tbl.tblPr.find(qn('w:tblW')).set(qn('w:w'), str(available))
    for col, width in zip(table.columns, widths, strict=True):
        col.width = Twips(width)
    # Repeat only an explicitly recognized header, never guess that data row zero is a heading.
    headers = block.get('header_rows') or []
    repeat = []
    for i in range(len(table.rows)):
        if i not in headers:
            break
        repeat.append(i)
    for i, row in enumerate(table.rows):
        if i in repeat:
            row._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = Twips(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                # Explicit leading avoids different Noto CJK auto-line metrics in
                # Word and LibreOffice. 14pt leaves room around ordinary 10pt text.
                paragraph.paragraph_format.line_spacing = Pt(14)
                paragraph.paragraph_format.keep_with_next = False
                for run in paragraph.runs:
                    run.font.size = Pt(10)
    return {'column_twips': widths, 'method': method, 'repeated_header_rows': repeat}


def validate_scan_word(content, recognition):
    """Reopen output; reject content loss, detached corrections and page overflow before delivery."""
    from io import BytesIO
    import re
    from docx import Document
    from docx.oxml.ns import qn
    clean = lambda s: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]', '', s)
    document = Document(BytesIO(content))
    expected = [(i, b) for i, p in enumerate(recognition['pages']) for b in p['blocks']
                if b['label'] == 'table' and b.get('rows')]
    if len(document.tables) != len(expected) or len(document.sections) != len(recognition['pages']):
        raise ValueError('scan_layout_structure_mismatch')
    bookmarks = document._element.xpath('.//w:bookmarkStart')
    names = [b.get(qn('w:name')) for b in bookmarks]
    if len(names) != len(set(names)):
        raise ValueError('scan_duplicate_correction_anchor')
    anchors = dict(zip(names, bookmarks, strict=True))
    checked_text = 0
    for page in recognition['pages']:
        for block in page['blocks']:
            label = block['label']
            if label in {'header','footer','number','page_number','image','chart','table'} or 'formula' in label:
                continue
            key = 'scan_' + block['block_id'].replace('-', '_')
            anchor = anchors.get(key)
            if anchor is None:
                raise ValueError('scan_text_anchor_missing')
            found = []
            for sibling in anchor.itersiblings():
                if sibling.tag == qn('w:bookmarkEnd') and sibling.get(qn('w:id')) == anchor.get(qn('w:id')):
                    break
                for node in sibling.iter():
                    if node.tag == qn('w:t'):
                        found.append(node.text or '')
                    elif node.tag == qn('w:br'):
                        found.append('\n')
                    elif node.tag == qn('w:tab'):
                        found.append('\t')
            if ''.join(found) != clean(block.get('text','')):
                raise ValueError('scan_text_changed')
            checked_text += 1
    section_index = 0
    checked_images = 0
    for element in document._element.body.iterchildren():
        section = document.sections[section_index]
        for inline in element.xpath('.//wp:inline'):
            extent = inline.find(qn('wp:extent'))
            width, height = int(extent.get('cx')), int(extent.get('cy'))
            if not (0 < width <= section.page_width-section.left_margin-section.right_margin+635
                    and 0 < height <= section.page_height-section.top_margin-section.bottom_margin+635):
                raise ValueError('scan_image_outside_page')
            checked_images += 1
        if element.tag == qn('w:p') and element.xpath('./w:pPr/w:sectPr'):
            section_index += 1
    reports = []
    checked_cells = 0
    for table, (page_index, block) in zip(document.tables, expected, strict=True):
        section = document.sections[page_index]
        available = usable_width_twips(section)
        widths = [col.width.twips for col in table.columns]
        if sum(widths) > available + 2 or min(widths) < 500 or table.autofit:
            raise ValueError('scan_table_width_invalid')
        if len(table.rows) != len(block['rows']) or len(table.columns) != max(map(len, block['rows'])):
            raise ValueError('scan_table_records_changed')
        expected_headers = []
        for i in range(len(table.rows)):
            if i not in (block.get('header_rows') or []):
                break
            expected_headers.append(i)
        actual_headers = [i for i, row in enumerate(table.rows) if row._tr.xpath('./w:trPr/w:tblHeader')]
        if actual_headers != expected_headers:
            raise ValueError('scan_table_header_changed')
        for i, values in enumerate(block['rows']):
            for j, value in enumerate(values):
                suffix = '＊' if block.get('cells') and block['cells'][i][j]['review_required'] else ''
                if table.cell(i, j).text != clean(value) + suffix:
                    raise ValueError('scan_table_text_changed')
                key = f'scan_{block["block_id"].replace("-", "_")}_r{i}_c{j}'
                if key not in names:
                    raise ValueError('scan_correction_anchor_missing')
                cell_width = table.cell(i, j)._tc.tcPr.find(qn('w:tcW'))
                if cell_width is None or int(cell_width.get(qn('w:w'))) != widths[j]:
                    raise ValueError('scan_cell_width_invalid')
                checked_cells += 1
        reports.append({'page': recognition['pages'][page_index]['page'], 'columns': len(widths),
                        'rows': len(table.rows), 'column_twips': widths, 'available_twips': available,
                        'repeated_header_rows': actual_headers})
    return {'version': 'ocr-readable-layout-v1', 'status': 'checked', 'checked_cells': checked_cells,
            'checked_images': checked_images,
            'checked_text_blocks': checked_text,
            'tables': reports, 'render_verified': False}
