import { randomUUID } from 'node:crypto';
import JSZip from 'jszip';

const WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const MAIN_PART = 'word/document.xml';

interface PreparedWordRuby {
  buffer: Buffer;
  annotations: Array<{ base: string; reading: string }>;
  pattern: RegExp;
}

/**
 * Prepare a temporary copy for Mammoth, whose Word reader skips w:ruby and
 * its descendants. A marker occupies the original run's inline position.
 * The caller replaces it with escaped HTML before sanitizing the preview.
 * The input archive must already have passed the ZIP size check.
 */
export async function prepareWordRuby(buffer: Buffer): Promise<PreparedWordRuby | null> {
  const zip = await JSZip.loadAsync(buffer);
  const source = await zip.file(MAIN_PART)?.async('string');
  if (!source || !/<(?:[\w.-]+:)?ruby[\s>]/.test(source)) {
    return null;
  }
  if (/<!DOCTYPE|<!ENTITY/i.test(source)) {
    throw new Error('Unsupported Word XML declaration');
  }

  const { JSDOM } = await import('jsdom');
  const dom = new JSDOM(source, { contentType: 'application/xml' });
  try {
    const document = dom.window.document;
    const annotations: PreparedWordRuby['annotations'] = [];
    const marker = `LIBRECHATRUBY${randomUUID().replaceAll('-', '')}N`;
    for (const ruby of Array.from(document.getElementsByTagNameNS(WORD, 'ruby'))) {
      const parent = ruby.parentElement;
      if (parent?.namespaceURI !== WORD || (parent.localName !== 'r' && parent.localName !== 'p')) {
        continue;
      }
      const childPart = (name: string) =>
        Array.from(ruby.children).find(
          (child) => child.namespaceURI === WORD && child.localName === name,
        );
      const text = (part: Element | undefined) =>
        Array.from(
          part?.getElementsByTagNameNS(WORD, 't') ?? [],
          (node) => node.textContent ?? '',
        ).join('');
      const basePart = childPart('rubyBase');
      const base = text(basePart);
      const reading = text(childPart('rt'));
      if (!base && !reading) {
        continue;
      }
      const prefix = ruby.prefix ? `${ruby.prefix}:` : '';
      const placeholder = document.createElementNS(WORD, `${prefix}t`);
      placeholder.textContent = `${marker}${annotations.length}END`;
      if (parent.localName === 'p') {
        // Some producers put ruby directly in a paragraph. Mammoth needs a run.
        const run = document.createElementNS(WORD, `${prefix}r`);
        const properties = basePart?.getElementsByTagNameNS(WORD, 'rPr')[0];
        if (properties) {
          run.appendChild(properties.cloneNode(true));
        }
        run.appendChild(placeholder);
        ruby.replaceWith(run);
      } else {
        ruby.replaceWith(placeholder);
      }
      annotations.push({ base, reading });
    }
    if (annotations.length === 0) {
      return null;
    }
    zip.file(MAIN_PART, new dom.window.XMLSerializer().serializeToString(document));
    const copy = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
    return { buffer: copy, annotations, pattern: new RegExp(`${marker}(\\d+)END`, 'g') };
  } finally {
    dom.window.close();
  }
}
