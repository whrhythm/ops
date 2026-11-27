import JSZip from 'jszip';
import { compareWordText, correctScanWord } from './OfficeWordPreview';

jest.mock('~/hooks', () => ({}));
const ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const p = (text: string) => `<w:p><w:r><w:t>${text}</w:t></w:r></w:p>`;

async function word(title: string, product: string, extra = '', media?: string) {
  const zip = new JSZip();
  if (media !== undefined) zip.file('word/media/image1.png', media);
  const cells = ['1', 'A001', product].map(v => '<w:tc>' + p(v) + '</w:tc>').join('');
  zip.file('word/document.xml', `<w:document xmlns:w="${ns}"><w:body>${p(title)}<w:tbl><w:tr>${cells}</w:tr></w:tbl>${extra}</w:body></w:document>`);
  const data = await zip.generateAsync({ type: 'arraybuffer' });
  return { size: data.byteLength, arrayBuffer: async () => data } as Blob;
}

it('reports only actual changes when the requested cell edit was missed', async () => {
  const result = await compareWordText(await word('原题', '文件夹'), await word('新题', '文件夹'));
  expect(result).toEqual([{ paragraph: 1, before: '原题', after: '新题' }]);
});
it('shows a changed operand in an editable fraction', async () => {
  const formula = (n: number) => '<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:f>' +
    '<m:num><m:r><m:t>1</m:t></m:r></m:num><m:den><m:r><m:t>' + n +
    '</m:t></m:r></m:den></m:f></m:oMath></w:p>';
  expect(await compareWordText(await word('题','A',formula(3)),await word('题','A',formula(5))))
    .toEqual([{paragraph:5,before:'(1) / (3)',after:'(1) / (5)'}]);
});
it('does not label an accompanying image replacement as a text-only edit', async () => {
  await expect(compareWordText(await word('原题','A','','old'),await word('新题','A','','new')))
    .rejects.toThrow('structure_changed');
});
it('identifies a changed cell after the row number and order ID', async () => {
  const result = await compareWordText(await word('原题', '文件夹'), await word('新题', '加厚文件夹'));
  expect(result).toHaveLength(2);
  expect(result[1]).toMatchObject({ table: { index: 1, row: 1, column: 3 }, before: '文件夹', after: '加厚文件夹' });
});
it('does not pair paragraphs by position when document structure changed', async () => {
  await expect(compareWordText(await word('原题', '文件夹'), await word('原题', '文件夹', p('注'))))
    .rejects.toThrow('structure_changed');
});

it.each([['0.1+0.2=0.3','verified'], ['12×5=50','mismatch'], ['a×b=50','manual'],
  ['1÷3=0.33','manual'], ['-1÷3=-0.33','manual'], ['2÷0=0','mismatch'], ['1÷3=0.38','mismatch']])(
  'locates a changed equation under the correct duplicate heading and checks %s', async (formula, expected) => {
    const heading = (name: string, level: number) => '<w:p><w:pPr><w:pStyle w:val="Heading'+level+'"/></w:pPr><w:r><w:t>'+name+'</w:t></w:r></w:p>';
    const equation = (value: string) => '<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>'+value+'</m:t></m:r></m:oMath></w:p>';
    const extra = (value: string) => heading('东区',1)+heading('报价',2)+equation('10×5=50')+
      heading('西区',1)+heading('报价',2)+equation(value);
    const changes = await compareWordText(await word('报告','A',extra('10×5=50')),
      await word('报告','A',extra(formula)));
    expect(changes).toHaveLength(1);
    expect(changes[0]).toMatchObject({section:'西区 › 报价', calculation:expected, after:formula});
  },
);

it('writes a correction only inside the selected scan bookmark and preserves the original', async () => {
  const sample = await word('原题', 'A001',
    '<w:p><w:r><w:t>〔待核对〕</w:t></w:r><w:bookmarkStart w:id="7" w:name="scan_p3_b2"/>' +
    '<w:r><w:t>旧字</w:t></w:r><w:bookmarkEnd w:id="7"/></w:p>');
  const corrected = await correctScanWord(sample, { scan_p3_b2: '新字 & 原稿' });
  const updated = await JSZip.loadAsync(corrected);
  const text = await updated.file('word/document.xml')!.async('string');
  expect(text).toContain('新字 &amp; 原稿');
  expect(text).toContain('〔待核对〕');
  expect(text).toContain('A001');
  expect(text).not.toContain('旧字');
  expect(await (await JSZip.loadAsync(await sample.arrayBuffer())).file('word/document.xml')!.async('string')).toContain('旧字');
  await expect(correctScanWord(sample, { scan_p3_b9: '新字' })).rejects.toThrow('scan_revision_target_missing');
});
