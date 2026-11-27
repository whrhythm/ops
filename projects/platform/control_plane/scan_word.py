"""Generate a separate, editable Word result for a structured scan."""
from contextlib import closing, contextmanager
from io import BytesIO
import re
from xml.sax import SAXParseException


@contextmanager
def open_source(source):
    """Use the same oriented source for recognition, page highlights and Word crops."""
    import pypdfium2
    from PIL import Image, ImageOps

    if source.startswith(b'%PDF-'):
        from pypdf import PdfReader
        if PdfReader(BytesIO(source)).is_encrypted:
            raise ValueError('encrypted_document')
        with pypdfium2.PdfDocument(source) as document:
            if not 1 <= len(document) <= 20:
                raise ValueError('page_count_out_of_range')
            yield document
        return
    with Image.open(BytesIO(source), formats=('PNG', 'JPEG')) as original:
        if original.width * original.height > 25_000_000 or getattr(original, 'n_frames', 1) != 1:
            raise ValueError('image_size_or_frame_count')
        with ImageOps.exif_transpose(original) as oriented:
            with oriented.convert('RGBA') as rgba:
                background = Image.new('RGB', oriented.size, 'white')
                background.paste(rgba, mask=rgba.getchannel('A'))
                with background:
                    yield background


def render_source_page(pdf, number, dpi):
    from PIL import Image

    if isinstance(pdf, Image.Image):
        if number != 1:
            raise ValueError('page_selection_out_of_range')
        return pdf.copy()
    with closing(pdf[number - 1]) as page:
        width, height = page.get_size()
        if width * height * (dpi / 72) ** 2 > 25_000_000:
            raise ValueError('page_too_large')
        with closing(page.render(scale=dpi / 72)) as bitmap:
            return bitmap.to_pil().convert('RGB')


def native_formulas(text):
    from lxml import etree
    from latex2mathml.converter import convert as mathml
    from mathml2omml import convert as omml

    if len(text) > 2000 or text.count('{') > 120:
        return []
    aligned = re.fullmatch(r'(?:\\scriptstyle\{)?\\begin\{aligned\}(.*?)\\end\{aligned\}\}?', text, re.S)
    if aligned:
        rows = ['']
        depth = 0
        environments = 0
        for token in re.findall(r'\\(?:begin|end)\{[a-zA-Z]+\}|\\\\|\\[{}&]|.', aligned[1], re.S):
            if token == r'\\' and depth == 0 and environments == 0:
                rows.append('')
                continue
            if token != '&' or depth or environments:
                rows[-1] += token
            depth += int(token == '{') - int(token == '}')
            environments += int(token.startswith(r'\begin{')) - int(token.startswith(r'\end{'))
        if environments or depth:
            return []
        expressions = [row.strip() for row in rows if row.strip()]
    else:
        expressions = [text]
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        elements = []
        for expression in expressions:
            xml = omml(mathml(expression))
            wrapper = etree.fromstring(f'<formula xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">{xml}</formula>'.encode(), parser)
            if len(wrapper) != 1 or etree.QName(wrapper[0]).localname != 'oMath' or not ''.join(wrapper[0].itertext()).strip():
                return []
            elements.append(wrapper[0])
        return elements
    except (ValueError, KeyError, NotImplementedError, etree.XMLSyntaxError, SAXParseException, AssertionError):
        return []
    except Exception as error:
        if type(error).__module__ == 'latex2mathml.exceptions':
            return []
        raise


def build_scan_word(source, recognition):
    from .scan_layout import configure_page, configure_table, validate_scan_word
    from docx import Document
    from docx.enum.section import WD_SECTION_START
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Emu, RGBColor, Pt

    def text(value):
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]', '', value)

    def warning(paragraph, label='〔待核对〕'):
        run = paragraph.add_run(label)
        run.bold = True
        run.font.color.rgb = RGBColor.from_string('975807')

    document = Document()
    normal = document.styles['Normal']
    normal.font.name = 'Noto Sans CJK SC'
    normal.font.size = Pt(10.5)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), 'Noto Sans CJK SC')
    bookmark_id = 0

    def editable_text(paragraph, key, value):
        nonlocal bookmark_id
        bookmark_id += 1
        start, end = OxmlElement('w:bookmarkStart'), OxmlElement('w:bookmarkEnd')
        start.set(qn('w:name'), 'scan_' + key.replace('-', '_'))
        for element in (start, end):
            element.set(qn('w:id'), str(bookmark_id))
        paragraph._p.append(start)
        paragraph.add_run(text(value))
        paragraph._p.append(end)

    with open_source(source) as pdf:
        for index, page in enumerate(recognition['pages']):
            section = document.add_section(WD_SECTION_START.NEW_PAGE) if index else document.sections[0]
            configure_page(section, page)
            document.add_paragraph(f'原文第 {page["page"]} 页', style='Subtitle')
            if page['review_required']:
                document.add_paragraph('请对照原扫描页，核对已标记的数字、表格和公式。')
            image = None
            for block in page['blocks']:
                label = block['label']
                if label in {'header', 'footer', 'number', 'page_number'}:
                    continue
                if label in {'image', 'chart'}:
                    if image is None:
                        image = render_source_page(pdf, page['page'], 150)
                    x1, y1, x2, y2 = block['bbox']
                    crop = image.crop((x1*image.width, y1*image.height, x2*image.width, y2*image.height))
                    png = BytesIO()
                    crop.save(png, 'PNG')
                    width = section.page_width-section.left_margin-section.right_margin
                    height = section.page_height-section.top_margin-section.bottom_margin-Pt(75)
                    scale = min(width/crop.width, height/crop.height, 914400/150)
                    document.add_picture(png, width=Emu(round(crop.width*scale)), height=Emu(round(crop.height*scale)))
                    continue
                if label == 'table' and block['rows']:
                    if block['review_reasons']:
                        warning(document.add_paragraph(), '〔请对照原表格〕')
                    table = document.add_table(rows=0, cols=max(map(len, block['rows'])))
                    table.style = 'Table Grid'
                    for row_index, values in enumerate(block['rows']):
                        row = table.add_row()
                        for column_index, value in enumerate(values):
                            cell = row.cells[column_index]
                            editable_text(cell.paragraphs[0], f"{block['block_id']}_r{row_index}_c{column_index}", value)
                            metadata = block.get('cells')
                            if metadata and metadata[row_index][column_index]['review_required']:
                                warning(cell.paragraphs[0], '＊')
                    configure_table(table, block, section)
                    continue
                content = text(block.get('text', ''))
                style = 'Heading 2' if label in {'title', 'doc_title', 'paragraph_title'} else None
                paragraph = document.add_paragraph(style=style)
                if block['review_reasons']:
                    warning(paragraph)
                # A recognizer's prediction is still a draft. Keep the warning next to the editable object.
                if 'formula' not in label:
                    editable_text(paragraph, block['block_id'], content)
                    continue
                for expression in block.get('formula_lines', [content]):
                    expression = text(expression)
                    equations = native_formulas(expression)
                    if equations:
                        for equation in equations:
                            document.add_paragraph()._p.append(equation)
                    else:
                        document.add_paragraph(expression)
    output = BytesIO()
    document.save(output)
    result = output.getvalue()
    validate_scan_word(result, recognition)
    return result
