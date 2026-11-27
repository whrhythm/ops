import { useEffect, useState } from 'react';
import type JSZip from 'jszip';
import { dataService } from 'librechat-data-provider';
import { useLocalize } from '~/hooks';
import WordPages from './WordPages';

const WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const DRAW = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const MATH = 'http://schemas.openxmlformats.org/officeDocument/2006/math';
const MAX_ARCHIVE = 16 * 1024 * 1024;
const MAX_XML = 1024 * 1024;
type TableCell = { text: string; colSpan: number };
type Block = { kind: 'text'; text: string; heading: boolean }
  | { kind: 'table'; rows: TableCell[][]; header: boolean }
  | { kind: 'image'; url: string }
  | { kind: 'formula'; text: string }
  | { kind: 'toc' };

/** Bound the actual output of each decompressor, even if the archive metadata lies. */
async function bytes(zip: JSZip, name: string, limit: number) {
  const entry = zip.file(name) as (JSZip.JSZipObject & {
    internalStream(type: 'uint8array'): JSZip.JSZipStreamHelper<Uint8Array<ArrayBuffer>>;
  }) | null;
  if (!entry) throw new Error('part_missing');
  return new Promise<Uint8Array<ArrayBuffer>>((resolve, reject) => {
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    const stream = entry.internalStream('uint8array');
    let length = 0;
    stream.on('data', (chunk) => {
      length += chunk.byteLength;
      if (length > limit) {
        stream.pause();
        reject(new Error('part_too_large'));
        return;
      }
      chunks.push(chunk);
    }).on('error', reject).on('end', () => {
      const output = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        output.set(chunk, offset);
        offset += chunk.byteLength;
      }
      resolve(output);
    }).resume();
  });
}

async function xml(zip: JSZip, name: string) {
  const source = new TextDecoder().decode(await bytes(zip, name, MAX_XML));
  if (/<!DOCTYPE|<!ENTITY/i.test(source)) throw new Error('unsafe_xml');
  const parsed = new DOMParser().parseFromString(source, 'application/xml');
  if (parsed.getElementsByTagName('parsererror').length) throw new Error('invalid_xml');
  return parsed;
}

export type WordTextChange = {
  paragraph: number;
  section?: string;
  calculation?: 'verified' | 'mismatch' | 'manual';
  table?: { index: number; row: number; column: number };
  before: string;
  after: string;
};

/** Check a literal, exact decimal equality without evaluating document text as code. */
function checkWordEquality(expression: string): 'verified' | 'mismatch' | 'manual' {
  const value = expression.normalize('NFKC').replace(/\s+/g, '').replaceAll('−', '-');
  const parts = /^(-?\d{1,16}(?:\.\d{1,8})?)([+*/×÷-])(-?\d{1,16}(?:\.\d{1,8})?)=(-?\d{1,16}(?:\.\d{1,8})?)$/.exec(value);
  if (!parts) return 'manual';
  const fixed = (s: string) => {
    const [whole, fraction = ''] = s.split('.');
    return BigInt(whole + fraction.padEnd(8, '0'));
  };
  const a = fixed(parts[1]), b = fixed(parts[3]), result = fixed(parts[4]);
  const scale = BigInt(100000000);
  // Compare in integer units. A displayed value rounded to the written precision
  // needs the author's rounding rule before it can be labelled correct or wrong.
  let numerator: bigint;
  let denominator = BigInt(1);
  switch (parts[2]) {
    case '+': numerator = a + b; break;
    case '-': numerator = a - b; break;
    case '*': case '×': numerator = a * b; denominator = scale; break;
    default:
      if (b === BigInt(0)) return 'mismatch';
      numerator = a * scale;
      denominator = b;
  }
  const difference = numerator - result * denominator;
  if (difference === BigInt(0)) return 'verified';
  const absolute = (n: bigint) => n < BigInt(0) ? -n : n;
  const precision = parts[4].split('.')[1]?.length ?? 0;
  const unit = BigInt(10 ** (8 - precision));
  return absolute(difference) * BigInt(2) <= absolute(denominator) * unit ? 'manual' : 'mismatch';
}

/** Resolve outline levels, including custom styles based on another heading style. */
function outlineLevel(paragraph: Element, styles: Map<string, Element>) {
  const prop = (node: Element, name: string) =>
    children(node, 'pPr').flatMap(p => children(p, name))[0]?.getAttributeNS(WORD, 'val');
  const level = (value?: string | null) => value && /^[0-9]$/.test(value) ? Number(value) + 1 : undefined;
  const direct = level(prop(paragraph, 'outlineLvl'));
  if (direct !== undefined) return direct;
  let id: string | null | undefined = prop(paragraph, 'pStyle');
  const visited = new Set<string>();
  while (id && !visited.has(id)) {
    visited.add(id);
    const style = styles.get(id);
    const explicit = style && level(prop(style, 'outlineLvl'));
    if (explicit !== undefined) return explicit;
    const name = style && children(style, 'name')[0]?.getAttributeNS(WORD, 'val');
    const heading = /^(?:heading|标题)\s*([1-9])$/i.exec(name ?? id);
    if (heading) return Number(heading[1]);
    id = style && children(style, 'basedOn')[0]?.getAttributeNS(WORD, 'val');
  }
}

/** Apply reviewed text only inside bookmarks written by the scan exporter. */
export async function correctScanWord(file: Blob, edits: Record<string, string>): Promise<Blob> {
  if (Object.keys(edits).length === 0) return file;
  if (file.size > MAX_ARCHIVE) throw new Error('archive_too_large');
  const { default: Zip } = await import('jszip');
  const zip = await Zip.loadAsync(await file.arrayBuffer());
  const part = 'word/document.xml';
  const document = await xml(zip, part);
  const pending = new Map(Object.entries(edits));
  for (const start of Array.from(document.getElementsByTagNameNS(WORD, 'bookmarkStart'))) {
    const name = start.getAttributeNS(WORD, 'name') ?? '';
    const value = pending.get(name);
    if (value === undefined) continue;
    const run = start.nextElementSibling;
    const end = run?.nextElementSibling;
    if (run?.localName !== 'r' || end?.localName !== 'bookmarkEnd' ||
        end.getAttributeNS(WORD, 'id') !== start.getAttributeNS(WORD, 'id')) {
      throw new Error('scan_revision_target_changed');
    }
    run.replaceChildren();
    for (const [index, line] of value.split(/\r\n?|\n/).entries()) {
      if (index) run.appendChild(document.createElementNS(WORD, 'w:br'));
      const text = document.createElementNS(WORD, 'w:t');
      text.setAttributeNS('http://www.w3.org/XML/1998/namespace', 'xml:space', 'preserve');
      text.textContent = line;
      run.appendChild(text);
    }
    pending.delete(name);
  }
  if (pending.size) throw new Error('scan_revision_target_missing');
  zip.file(part, new XMLSerializer().serializeToString(document));
  return zip.generateAsync({ type: 'blob', compression: 'DEFLATE', mimeType: file.type });
}

/** Compare aligned paragraphs and cells. Never align unrelated rows after a structural edit. */
export async function compareWordText(before: Blob, after: Blob, sectionReview = true): Promise<WordTextChange[]> {
  if (Math.max(before.size, after.size) > MAX_ARCHIVE) throw new Error('archive_too_large');
  const { default: Zip } = await import('jszip');
  const packages = await Promise.all([before, after].map(async file => Zip.loadAsync(await file.arrayBuffer())));
  const parts = packages.map(zip => Object.keys(zip.files).filter(name =>
    !zip.files[name].dir && name !== 'word/document.xml' && !name.startsWith('docProps/')).sort());
  if (parts[0].length !== parts[1].length || parts[0].some((name, index) => name !== parts[1][index])) {
    throw new Error('structure_changed');
  }
  let remaining = MAX_ARCHIVE * 3;
  for (const name of parts[0]) {
    const [left, right] = await Promise.all(packages.map(zip => bytes(zip, name, remaining)));
    if (left.length !== right.length || left.some((value, index) => value !== right[index])) {
      throw new Error('structure_changed');
    }
    remaining -= left.length;
  }
  const [a, b] = await Promise.all(packages.map(zip => xml(zip, 'word/document.xml')));
  const paragraphs = [a, b].map(doc => Array.from(doc.getElementsByTagNameNS(WORD, 'p')));
  if (paragraphs[0].length !== paragraphs[1].length) throw new Error('structure_changed');
  const tables = Array.from(a.getElementsByTagNameNS(WORD, 'tbl'));
  const styles = new Map<string, Element>();
  if (sectionReview && packages[0].file('word/styles.xml')) {
    const definitions = await xml(packages[0], 'word/styles.xml');
    for (const style of Array.from(definitions.getElementsByTagNameNS(WORD, 'style'))) {
      const id = style.getAttributeNS(WORD, 'styleId');
      if (id && style.getAttributeNS(WORD, 'type') === 'paragraph') styles.set(id, style);
    }
  }
  const headings: string[] = [];
  const changes: WordTextChange[] = [];
  paragraphs[0].forEach((p, i) => {
    const q = paragraphs[1][i];
    const oldValue = paragraphWithMath(p), newValue = paragraphWithMath(q);
    if (sectionReview && p.parentElement?.localName === 'body') {
      const depth = outlineLevel(p, styles);
      if (depth && depth <= 9) {
        headings.length = depth - 1;
        headings.push(oldValue.trim());
      }
    }
    if (oldValue === newValue) return;
    const cell = p.parentElement, row = cell?.parentElement, table = row?.parentElement;
    const location = cell?.localName === 'tc' && row?.localName === 'tr' && table?.localName === 'tbl'
      ? { index: tables.indexOf(table) + 1, row: children(table, 'tr').indexOf(row) + 1,
          column: children(row, 'tc').indexOf(cell) + 1 } : undefined;
    const section = headings.filter(Boolean).join(' › ');
    const equations = sectionReview ? Array.from(q.getElementsByTagNameNS(MATH, 'oMath')) : [];
    const checks = equations.filter(eq => mathText(eq).includes('=')).map(eq => checkWordEquality(mathText(eq)));
    const calculation = checks.includes('mismatch') ? 'mismatch' : checks.includes('manual') ? 'manual'
      : checks.length ? 'verified' : undefined;
    changes.push({ paragraph: i + 1, ...(section ? { section } : {}), ...(calculation ? { calculation } : {}),
      table: location, before: oldValue, after: newValue });
    // Leave formula structures intact for the equality check. Only operands with
    // a visible difference may be removed from the structural comparison.
    const newFormulas = q.getElementsByTagNameNS(MATH, 'oMath');
    for (const [index, original] of Array.from(p.getElementsByTagNameNS(MATH, 'oMath')).entries()) {
      const updated = newFormulas[index];
      if (!updated || mathText(original) === mathText(updated)) continue;
      for (const math of [original, updated]) {
        for (const node of Array.from(math.getElementsByTagNameNS(MATH, 't'))) node.textContent = '';
      }
    }
  });
  // A comparison of text nodes cannot explain changes to fields, geometry or cell order.
  for (const doc of [a, b]) {
    for (const node of Array.from(doc.getElementsByTagNameNS(WORD, 't'))) node.textContent = '';
  }
  if (!a.documentElement.isEqualNode(b.documentElement)) throw new Error('structure_changed');
  return changes;
}

const textOf = (part: Element) => Array.from(part.getElementsByTagNameNS(WORD, 't'), (node) => node.textContent ?? '').join('');
const children = (part: Element, name: string) => Array.from(part.children).filter((node) => node.namespaceURI === WORD && node.localName === name);

function paragraphWithMath(element: Element): string {
  if (element.namespaceURI === MATH && element.localName === 'oMath') return mathText(element);
  if (element.namespaceURI === WORD && element.localName === 't') return element.textContent ?? '';
  return Array.from(element.children, paragraphWithMath).join('');
}

function readTableCell(cell: Element): TableCell {
  const properties = children(cell, 'tcPr')[0];
  const gridSpan = properties && children(properties, 'gridSpan')[0]?.getAttributeNS(WORD, 'val');
  const span = Number(gridSpan ?? '1');
  return {
    text: children(cell, 'p').map(paragraphWithMath).join('\n'),
    colSpan: Number.isSafeInteger(span) && span >= 1 && span <= 32 ? span : 1,
  };
}

function mathText(element: Element, depth = 0): string {
  if (depth > 20) return '…';
  const scoped = Array.from(element.children).filter((node) => node.namespaceURI === MATH);
  const argument = (name: string) => {
    const child = scoped.find((node) => node.localName === name);
    return child ? mathText(child, depth + 1) : '';
  };
  switch (element.localName) {
    case 't': return element.textContent ?? '';
    case 'f': return `(${argument('num')}) / (${argument('den')})`;
    case 'rad': return `${argument('deg')}√(${argument('e')})`;
    case 'd': return argument('e');
    case 'm': return `[ ${scoped.filter((child) => child.localName === 'mr')
      .map((child) => mathText(child, depth + 1)).join(' ; ')} ]`;
    case 'mr': return scoped.filter((child) => child.localName === 'e')
      .map((child) => mathText(child, depth + 1)).join(', ');
    case 'sSup': return `${argument('e')}^(${argument('sup')})`;
    case 'sSub': return `${argument('e')}_(${argument('sub')})`;
    default: return scoped.filter((child) => !child.localName.endsWith('Pr'))
      .map((child) => mathText(child, depth + 1)).join('');
  }
}

function hasTocField(part: Element) {
  const fragments = Array.from(part.getElementsByTagNameNS(WORD, 'instrText'), (child) => child.textContent ?? '');
  const simple = Array.from(part.getElementsByTagNameNS(WORD, 'fldSimple'),
    (child) => child.getAttributeNS(WORD, 'instr') ?? '');
  return /^\s*TOC(?:\s|$)/i.test(fragments.join('')) || simple.some((field) => /^\s*TOC(?:\s|$)/i.test(field));
}

async function readDocument(file: Blob, objectUrl: (blob: Blob) => string) {
  if (file.size > MAX_ARCHIVE) throw new Error('archive_too_large');
  const { default: Zip } = await import('jszip');
  const zip = await Zip.loadAsync(await file.arrayBuffer());
  const document = await xml(zip, 'word/document.xml');
  const body = document.getElementsByTagNameNS(WORD, 'body')[0];
  if (!body || body.children.length > 600) throw new Error('unsupported_body');
  const relationships = zip.file('word/_rels/document.xml.rels')
    ? await xml(zip, 'word/_rels/document.xml.rels') : null;
  const media = new Map<string, string>();
  for (const relation of Array.from(relationships?.documentElement.children ?? [])) {
    const target = relation.getAttribute('Target') ?? '';
    const id = relation.getAttribute('Id');
    if (id && relation.getAttribute('Type') === `${REL}/image` &&
        relation.getAttribute('TargetMode') !== 'External' && /^media\/[\w.-]+\.(png|jpe?g)$/i.test(target)) {
      media.set(id, `word/${target}`);
    }
  }
  const blocks: Block[] = [];
  let imageCount = 0;
  for (const part of Array.from(body.children)) {
    if (part.namespaceURI !== WORD) continue;
    const toc = hasTocField(part);
    if (toc) blocks.push({ kind: 'toc' });
    if (toc && part.localName === 'sdt') {
      const content = children(part, 'sdtContent')[0];
      for (const entry of content ? children(content, 'p') : []) {
        const text = textOf(entry);
        if (text) blocks.push({ kind: 'text', text, heading: false });
      }
    }
    if (part.localName === 'tbl') {
      const rows = children(part, 'tr');
      if (rows.length > 250) throw new Error('table_too_large');
      blocks.push({ kind: 'table', header: !!rows[0]?.getElementsByTagNameNS(WORD, 'tblHeader').length,
        rows: rows.map((row) => children(row, 'tc').map(readTableCell)) });
    }
    if (part.localName !== 'p') continue;
    const text = textOf(part);
    const style = part.getElementsByTagNameNS(WORD, 'pStyle')[0]?.getAttributeNS(WORD, 'val') ?? '';
    if (text) blocks.push({ kind: 'text', text, heading: /^(title|heading\d)$/i.test(style) });
    for (const formula of Array.from(part.getElementsByTagNameNS(MATH, 'oMath'))) {
      const expression = mathText(formula);
      if (expression.trim()) blocks.push({ kind: 'formula', text: expression });
    }
    for (const drawing of Array.from(part.getElementsByTagNameNS(DRAW, 'blip'))) {
      const location = media.get(drawing.getAttributeNS(REL, 'embed') ?? '');
      if (!location) continue;
      if (++imageCount > 8) throw new Error('too_many_images');
      const raw = await bytes(zip, location, 2 * 1024 * 1024);
      const isPng = /\.png$/i.test(location);
      if (isPng ? !(raw[0] === 137 && raw[1] === 80 && raw[2] === 78 && raw[3] === 71)
        : !(raw[0] === 255 && raw[1] === 216 && raw[2] === 255)) continue;
      blocks.push({ kind: 'image', url: objectUrl(new Blob([raw], { type: isPng ? 'image/png' : 'image/jpeg' })) });
    }
  }
  return blocks;
}

function WordTextFallback({ filepath }: { filepath: string }) {
  const t = useLocalize();
  const [blocks, setBlocks] = useState<Block[]>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    const urls: string[] = [];
    setBlocks(undefined);
    setFailed(false);
    dataService.getCodeOutputDownload(filepath).then(({ data }) => {
      if (!(data instanceof Blob) || !active) throw new Error('preview_unavailable');
      return readDocument(data, (image) => {
        if (!active) throw new Error('preview_closed');
        const url = URL.createObjectURL(image);
        urls.push(url);
        return url;
      });
    }).then((result) => { if (active) setBlocks(result); })
      .catch(() => { if (active) setFailed(true); });
    return () => {
      active = false;
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [filepath]);
  return <div className="min-h-0 space-y-4 overflow-auto pb-3">
          {failed && <p role="alert">{t('com_ui_preview_unavailable')}</p>}
          {!blocks && !failed && <p role="status">{t('com_ui_loading')}</p>}
          {blocks?.map((block, index) => {
            if (block.kind === 'image') return <img key={index} src={block.url} alt={t('com_office_word_preview_image')} className="mx-auto max-h-80 max-w-full object-contain" />;
            if (block.kind === 'formula') return <p key={index} data-testid="office-word-formula" className="break-words font-mono" aria-label={t('com_office_word_preview_formula')}>{block.text}</p>;
            if (block.kind === 'toc') return <p key={index} className="text-text-secondary text-sm">{t('com_office_word_preview_toc')}</p>;
            if (block.kind === 'text') return block.heading
              ? <h3 key={index} className="break-words text-base font-semibold">{block.text}</h3>
              : <p key={index} className="break-words">{block.text}</p>;
            return <div key={index} className="overflow-x-auto"><table className="w-full text-sm"><tbody>
              {block.rows.map((row, rowId) => <tr key={rowId}>{row.map((cell, columnId) => {
                const Cell = rowId === 0 && block.header ? 'th' : 'td';
                return <Cell key={columnId} colSpan={cell.colSpan} className="border-border-light border px-2 py-1 text-left whitespace-pre-wrap">{cell.text}</Cell>;
              })}</tr>)}
            </tbody></table></div>;
          })}
        </div>;
}

export default function OfficeWordPreview({ filename, filepath, onOpen }: { filename: string; filepath: string; onOpen?: () => void }) {
  return <WordPages filename={filename} onOpen={onOpen} fallback={<WordTextFallback filepath={filepath} />}
    load={async () => (await dataService.getCodeOutputDownload(filepath)).data as Blob} />;
}
