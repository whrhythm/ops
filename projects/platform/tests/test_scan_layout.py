"""Focused document tests runnable with the bundled document Python runtime."""
from copy import deepcopy
from io import BytesIO
import unittest
from PIL import Image
from docx import Document
from control_plane.scan_layout import column_widths, validate_scan_word
from control_plane.scan_word import build_scan_word


def source():
    out=BytesIO();Image.new('RGB',(800,600),'white').save(out,'PNG');return out.getvalue()


def sample(wide=False, headers=False):
    rows=[['编号','名称','金额'],['A001','原文必须保留的较长名称，含中文和 English','1234.50']]
    block={'block_id':'p1-b1','label':'table','rows':rows,'review_reasons':[]}
    if headers:block['header_rows']=[0]
    return {'pages':[{'page':1,'source_size':[1200,800] if wide else [800,1200],
        'review_required':False,'blocks':[block]}]}


class ScanLayoutTest(unittest.TestCase):
    def test_geometry_preserves_proportions_and_protects_narrow_columns(self):
        b={'rows':[['部门','序号','说明'],['','1','长说明'*100]],'cells':[
            [{'bbox':[0,0,.1,.2]},{'bbox':[.1,0,.15,.2]},{'bbox':[.15,0,1,.2]}]]}
        widths,method=column_widths(b,10000)
        self.assertEqual(method,'source_cell_geometry');self.assertEqual(sum(widths),10000)
        self.assertGreaterEqual(min(widths),560);self.assertGreater(widths[2],widths[0]*3)

    def test_invalid_geometry_falls_back_without_losing_text(self):
        b={'rows':[['编号','说明'],['1','长说明'*30]],'cells':[[{'bbox':[float('nan'),0,1,1]},{}]]}
        widths,method=column_widths(b,7000)
        self.assertEqual(method,'text_length_fallback');self.assertEqual(sum(widths),7000)
        self.assertGreater(widths[1],widths[0])

    def test_landscape_and_all_text_bookmarks_survive(self):
        r=sample(True);out=build_scan_word(source(),r);doc=Document(BytesIO(out))
        self.assertGreater(doc.sections[0].page_width,doc.sections[0].page_height)
        self.assertEqual(doc.tables[0].cell(1,1).text,r['pages'][0]['blocks'][0]['rows'][1][1])
        self.assertEqual(validate_scan_word(out,r)['checked_cells'],6)

    def test_mixed_page_directions_have_independent_sections(self):
        r=sample(True);p=deepcopy(sample(False)['pages'][0]);p['page']=2;p['blocks'][0]['block_id']='p2-b1';r['pages'].append(p)
        doc=Document(BytesIO(build_scan_word(source(),r)))
        self.assertEqual(len(doc.sections),2)
        self.assertGreater(doc.sections[0].page_width,doc.sections[0].page_height)
        self.assertLess(doc.sections[1].page_width,doc.sections[1].page_height)

    def test_only_explicit_headers_repeat_and_no_row_is_duplicated(self):
        for explicit in (False,True):
            r=sample(headers=explicit);r['pages'][0]['blocks'][0]['rows'] += [[str(i),'item','1.00'] for i in range(65)]
            doc=Document(BytesIO(build_scan_word(source(),r)))
            self.assertEqual(len(doc.tables[0].rows),67)
            self.assertEqual(len(doc.tables[0].rows[0]._tr.xpath('./w:trPr/w:tblHeader')),int(explicit))

    def test_content_change_is_rejected_before_delivery(self):
        r=sample();doc=Document(BytesIO(build_scan_word(source(),r)));doc.tables[0].cell(1,2).text='999'
        out=BytesIO();doc.save(out)
        with self.assertRaisesRegex(ValueError,'scan_table_text_changed'):validate_scan_word(out.getvalue(),r)

    def test_out_of_page_table_is_rejected(self):
        from docx.shared import Inches
        r=sample();doc=Document(BytesIO(build_scan_word(source(),r)));doc.tables[0].columns[0].width=Inches(20)
        out=BytesIO();doc.save(out)
        with self.assertRaisesRegex(ValueError,'scan_table_width_invalid'):validate_scan_word(out.getvalue(),r)

    def test_unreadable_column_count_fails_without_tiny_font(self):
        with self.assertRaises(ValueError):column_widths({'rows':[['正文']*30]},10000)

    def test_table_headers_require_explicit_html_markup(self):
        from control_plane.office_scan import Table
        self.assertEqual(Table('<table><tr><td>first data</td></tr></table>').header_rows,[])
        self.assertEqual(Table('<table><thead><tr><td>head</td></tr></thead><tr><td>data</td></tr></table>').header_rows,[0])
        self.assertEqual(Table('<table><tr><th>head</th></tr><tr><td>data</td></tr></table>').header_rows,[0])

    def test_images_fit_their_section_and_oversized_output_is_rejected(self):
        from docx.shared import Inches
        r=sample();r['pages'][0]['blocks'].append({'label':'image','bbox':[0,0,1,1]})
        out=build_scan_word(source(),r);self.assertEqual(validate_scan_word(out,r)['checked_images'],1)
        doc=Document(BytesIO(out));doc.inline_shapes[0].width=Inches(20)
        changed=BytesIO();doc.save(changed)
        with self.assertRaisesRegex(ValueError,'scan_image_outside_page'):validate_scan_word(changed.getvalue(),r)

    def test_body_text_and_correction_anchor_are_checked(self):
        r=sample();r['pages'][0]['blocks'].append({'label':'text','block_id':'p1-b2',
            'text':'正文保持\n第二行\t含制表符','review_reasons':[]})
        content=build_scan_word(source(),r)
        self.assertEqual(validate_scan_word(content,r)['checked_text_blocks'],1)
        doc=Document(BytesIO(content));doc.paragraphs[-1].runs[0].text='被更改'
        changed=BytesIO();doc.save(changed)
        with self.assertRaisesRegex(ValueError,'scan_text_changed'):validate_scan_word(changed.getvalue(),r)

if __name__=='__main__':unittest.main()
