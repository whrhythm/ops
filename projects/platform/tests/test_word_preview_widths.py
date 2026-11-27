from io import BytesIO
import importlib.util
from pathlib import Path
import unittest
from zipfile import ZipFile
from docx import Document
from docx.shared import Twips
from docx.oxml.ns import qn

spec=importlib.util.spec_from_file_location('font_metrics',Path(__file__).resolve().parents[1]/'deploy/word-pages/font_metrics.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PreviewWidthsTest(unittest.TestCase):
    def sample(self,autofit=False,merge=False,inconsistent=False):
        d=Document();t=d.add_table(rows=3,cols=3);t.autofit=autofit
        for i,row in enumerate(t.rows):
            for c,w,value in zip(row.cells,[1000,6300,1800],['L001','核对事项','10']):c.width=Twips(w);c.text=value
        if inconsistent:t.cell(2,0).width=Twips(1200)
        if merge:t.cell(0,0).merge(t.cell(0,1))
        b=BytesIO();d.save(b);return b.getvalue()

    def test_fixed_consistent_cell_widths_drive_preview_and_preserve_all_other_parts(self):
        source=self.sample();result=module.calibrated_document(source)
        self.assertEqual([c.width.twips for c in Document(BytesIO(result)).tables[0].columns],[1000,6300,1800])
        with ZipFile(BytesIO(source)) as before,ZipFile(BytesIO(result)) as after:
            for n in before.namelist():
                if n!='word/document.xml':self.assertEqual(before.read(n),after.read(n))
        self.assertEqual(module.calibrated_document(result),result)
        a,b=[Document(BytesIO(x)) for x in (source,result)]
        self.assertEqual([[c.text for c in r.cells] for r in a.tables[0].rows],[[c.text for c in r.cells] for r in b.tables[0].rows])

    def test_ambiguous_or_autofit_tables_are_not_reinterpreted(self):
        for options in [dict(autofit=True),dict(merge=True),dict(inconsistent=True)]:
            source=self.sample(**options);self.assertEqual(module.calibrated_document(source),source)

if __name__=='__main__':unittest.main()
