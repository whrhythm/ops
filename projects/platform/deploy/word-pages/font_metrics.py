"""Match Word's measured auto-leading for plain DengXian text when using open fonts."""
from io import BytesIO
from zipfile import ZipFile
from lxml import etree


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SANS = {'DengXian', '等线'}


def calibrated_document(source):
    with ZipFile(BytesIO(source)) as original:
        xml = etree.fromstring(original.read('word/document.xml'), etree.XMLParser(resolve_entities=False, no_network=True))
        changed = False
        # Word prefers explicit cell widths in fixed rectangular tables. Writer
        # may instead prefer a stale equal-width grid left by python-docx.
        # Align only the temporary rendering copy to the consistent cell intent.
        for table in xml.iter(W + 'tbl'):
            layout = table.find(W + 'tblPr/' + W + 'tblLayout')
            grid = table.find(W + 'tblGrid')
            if layout is None or layout.get(W + 'type') != 'fixed' or grid is None:
                continue
            columns = grid.findall(W + 'gridCol')
            rows = table.findall(W + 'tr')
            if not columns or not rows or table.findall('.//' + W + 'gridSpan') or table.findall('.//' + W + 'vMerge'):
                continue
            widths = []
            for row in rows:
                cells = row.findall(W + 'tc')
                values = []
                for cell in cells:
                    cw = cell.find(W + 'tcPr/' + W + 'tcW')
                    raw = '' if cw is None else cw.get(W + 'w', '')
                    if cw is None or cw.get(W + 'type') != 'dxa' or not raw.isdecimal() or not 0 < int(raw) < 40000:
                        break
                    values.append(int(raw))
                if len(cells) != len(columns) or len(values) != len(columns):
                    break
                widths.append(values)
            else:
                original_widths = [c.get(W + 'w', '') for c in columns]
                if (all(n.isdecimal() for n in original_widths) and widths and all(row == widths[0] for row in widths)
                        and .8 <= sum(widths[0]) / max(1, sum(map(int, original_widths))) <= 1.1
                        and list(map(int, original_widths)) != widths[0]):
                    for col, width in zip(columns, widths[0]):
                        col.set(W + 'w', str(width))
                    changed = True
        for p in xml.iter(W + 'p'):
            spacing = p.find(W + 'pPr/' + W + 'spacing')
            if spacing is None or spacing.get(W + 'lineRule') != 'auto':
                continue
            leading = spacing.get(W + 'line', '')
            if not leading.isdecimal() or not 240 <= int(leading) <= 360:
                continue
            # Only ordinary homogeneous text. Inline figures, equations, fields,
            # ruby and elevated glyphs need their own intrinsic line boxes.
            if any(n.tag not in (W + 'pPr', W + 'r') for n in p):
                continue
            runs = p.findall(W + 'r')
            if not runs or any(any(n.tag not in (W + 't', W + 'rPr') for n in r) for r in runs):
                continue
            sizes = set()
            for run in runs:
                props = run.find(W + 'rPr')
                fonts = run.find(W + 'rPr/' + W + 'rFonts')
                size = run.find(W + 'rPr/' + W + 'sz')
                if (props is None or fonts is None or fonts.get(W + 'eastAsia') not in SANS or
                        fonts.get(W + 'ascii') != 'Calibri' or size is None or
                        any(props.find(W + n) is not None for n in ('vertAlign', 'position', 'fitText', 'rStyle'))):
                    break
                sizes.add(size.get(W + 'val', ''))
            else:
                if len(sizes) == 1:
                    half_points = sizes.pop()
                    if half_points.isdecimal() and 18 <= int(half_points) <= 28:
                        twips = round(int(half_points) * 10 * 1.355 * int(leading) / 240)
                        spacing.set(W + 'line', str(twips))
                        spacing.set(W + 'lineRule', 'exact')
                        changed = True
        if not changed:
            return source
        result = BytesIO()
        with ZipFile(result, 'w') as package:
            for entry in original.infolist():
                package.writestr(entry, etree.tostring(xml, encoding='UTF-8', xml_declaration=True, standalone=True)
                                 if entry.filename == 'word/document.xml' else original.read(entry))
        return result.getvalue()
