"""Deterministic document boundaries and repair preservation, no real user files."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import importlib.util
import unittest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches,Twips

spec=importlib.util.spec_from_file_location('word_quality',Path(__file__).with_name('word_quality.py'))
quality=importlib.util.module_from_spec(spec);spec.loader.exec_module(quality)

def encoded(doc):
    out=BytesIO();doc.save(out);return out.getvalue()

def replace_part(source,name,value):
    output=BytesIO()
    with ZipFile(BytesIO(source)) as old, ZipFile(output,'w') as new:
        for p in old.infolist():new.writestr(p,value if p.filename==name else old.read(p))
    return output.getvalue()

class WordQualityTest(unittest.TestCase):
    def document(self):
        doc=Document();doc.add_heading('收入分析',0);doc.add_paragraph('收入 2800，成本 1700，毛利 1100。')
        t=doc.add_table(rows=3,cols=2)
        for row,values in zip(t.rows,[['编号','金额'],['A001','2800'],['A002','1700']]):
            for c,s in zip(row.cells,values):c.text=s
        return doc

    def wide(self,doc):
        t=doc.tables[0]
        for c in t.columns:c.width=Inches(7)
        for row in t.rows:
            for c in row.cells:c.width=Inches(7)
        return doc

    def test_clean_is_byte_identical_and_deterministic(self):
        source=encoded(self.document())
        out,report=quality.inspect(source,True)
        self.assertEqual(out,source);self.assertEqual(report['status'],'checked')
        self.assertFalse(report['render_verified']);self.assertFalse(report['source_facts_verified'])

    def test_wide_table_repaired_without_content_or_part_loss(self):
        source=encoded(self.wide(self.document()))
        out,report=quality.inspect(source,True)
        self.assertEqual(report['repairs'],['table_outside_page'])
        self.assertEqual(quality.inspect(source,True)[0],out)
        a,b=[quality.package(v)[1] for v in (source,out)]
        self.assertEqual(quality.protected(quality.parse(a['word/document.xml'])),quality.protected(quality.parse(b['word/document.xml'])))
        self.assertTrue(all(b[n]==v for n,v in a.items() if n!='word/document.xml'))
        self.assertEqual(quality.inspect(out,False)[0],out)

    def test_revision_never_silently_reflows(self):
        with self.assertRaisesRegex(quality.Rejected,'table_outside_page'):
            quality.inspect(encoded(self.wide(self.document())),False)

    def test_landscape_and_mixed_sections_use_their_own_bounds(self):
        doc=self.document();a=doc.sections[0];a.page_width=Inches(11.7);a.page_height=Inches(8.3)
        for c in doc.tables[0].columns:c.width=Inches(4)
        for row in doc.tables[0].rows:
            for c in row.cells:c.width=Inches(4)
        section=doc.add_section();section.page_width=Inches(8.3);section.page_height=Inches(11.7)
        t=doc.add_table(rows=1,cols=2)
        for c in t.columns:c.width=Inches(4)
        for c in t.rows[0].cells:c.width=Inches(4)
        out,report=quality.inspect(encoded(doc),True)
        result=Document(BytesIO(out))
        self.assertEqual(report['repairs'],['table_outside_page'])
        self.assertEqual(result.tables[0].columns[0].width,Inches(4))
        self.assertLess(result.tables[1].columns[0].width,Inches(4))

    def test_equations_and_explicit_headers_survive(self):
        doc=self.wide(self.document());p=doc.add_paragraph();math=OxmlElement('m:oMath');r=OxmlElement('m:r');t=OxmlElement('m:t');t.text='10×5=50';r.append(t);math.append(r);p._p.append(math)
        doc.tables[0].rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
        out,report=quality.inspect(encoded(doc),True)
        self.assertEqual(report['counts']['formulas'],1)
        self.assertIn('10×5=50',Document(BytesIO(out))._element.xml)
        self.assertTrue(Document(BytesIO(out)).tables[0].rows[0]._tr.xpath('./w:trPr/w:tblHeader'))

    def test_exact_row_height_becomes_minimum_only_for_new_document(self):
        doc=self.document();height=OxmlElement('w:trHeight');height.set(qn('w:val'),'100');height.set(qn('w:hRule'),'exact');doc.tables[0].rows[1]._tr.get_or_add_trPr().append(height)
        source=encoded(doc)
        with self.assertRaisesRegex(quality.Rejected,'exact_table_row'):quality.inspect(source,False)
        out,report=quality.inspect(source,True)
        self.assertEqual(report['repairs'],['exact_table_row_may_clip'])

    def test_merged_overflow_is_rejected_instead_of_flattened(self):
        doc=self.wide(self.document());doc.tables[0].cell(0,0).merge(doc.tables[0].cell(0,1))
        with self.assertRaisesRegex(quality.Rejected,'merged_table_overflow'):quality.inspect(encoded(doc),True)

    def test_unreadable_wide_table_rejected_not_tiny_font(self):
        doc=Document();t=doc.add_table(rows=1,cols=20)
        for c in t.columns:c.width=Inches(1)
        for c in t.rows[0].cells:c.width=Inches(1);c.text='项目'
        with self.assertRaisesRegex(quality.Rejected,'cannot_fit'):quality.inspect(encoded(doc),True)

    def test_unsafe_xml_and_external_resources_rejected(self):
        source=encoded(self.document())
        with self.assertRaisesRegex(quality.Rejected,'unsafe_xml'):
            quality.inspect(replace_part(source,'word/document.xml',b'<!DOCTYPE a><a/>'),True)
        rel=b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rX" Type="image" Target="http://internal/secret" TargetMode="External"/></Relationships>'
        with self.assertRaisesRegex(quality.Rejected,'external_resource'):
            quality.inspect(replace_part(source,'word/_rels/document.xml.rels',rel),True)

    def test_duplicate_package_entries_rejected(self):
        source=encoded(self.document());sink=BytesIO(source)
        with ZipFile(sink,'a') as z:z.writestr('word/document.xml',b'<x/>')
        with self.assertRaisesRegex(quality.Rejected,'package_limit'):quality.inspect(sink.getvalue(),True)

    def test_large_image_fits_and_image_bytes_are_preserved(self):
        from PIL import Image
        png=BytesIO();Image.new('RGB',(20,10),'blue').save(png,format='PNG');png.seek(0)
        doc=self.document();doc.add_picture(png,width=Inches(10))
        source=encoded(doc);out,report=quality.inspect(source,True)
        self.assertIn('image_outside_page',report['repairs'])
        self.assertEqual(report['counts']['images'],1)
        a,b=[quality.package(v)[1] for v in (source,out)]
        self.assertTrue(all(b[n]==raw for n,raw in a.items() if n.startswith('word/media/')))

    def test_empty_document_is_not_published_as_success(self):
        with self.assertRaisesRegex(quality.Rejected,'empty_document'):quality.inspect(encoded(Document()),True)

if __name__=='__main__':unittest.main()
