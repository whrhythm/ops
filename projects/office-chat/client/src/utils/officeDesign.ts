import type { TFile, TMessage } from 'librechat-data-provider';

const themes = {
  garden: ['#204837', '#dcebd8', '#f7f8ee'],
  ink: ['#213957', '#e2ebf5', '#f7f9fb'],
  sunset: ['#763923', '#f5dfc5', '#fff8ed'],
} as const;

type Photo = { ref: number | string; caption: string; focusX?: number; focusY?: number };
type Design = {
  version: 1;
  orientation: 'portrait' | 'landscape';
  theme: keyof typeof themes;
  layout: 'feature' | 'gallery';
  title: string;
  subtitle: string;
  photos: Photo[];
  noteTitle: string;
  note: string;
  handwriting: boolean;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value != null && typeof value === 'object' && !Array.isArray(value);
const isText = (value: unknown, max: number): value is string =>
  typeof value === 'string' && Array.from(value).length <= max;

export function parseOfficeDesign(source: string): Design | null {
  if (source.length > 12_000) {
    return null;
  }
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    return null;
  }
  if (!isRecord(value)) {
    return null;
  }
  const {
    version,
    orientation,
    theme,
    layout,
    title,
    subtitle,
    photos,
    noteTitle,
    note,
    handwriting,
  } = value;
  if (
    version !== 1 ||
    (orientation !== 'portrait' && orientation !== 'landscape') ||
    (theme !== 'garden' && theme !== 'ink' && theme !== 'sunset') ||
    (layout !== 'feature' && layout !== 'gallery') ||
    !isText(title, 26) ||
    !title.trim() ||
    !isText(subtitle, 48) ||
    !isText(noteTitle, 14) ||
    !isText(note, 150) ||
    typeof handwriting !== 'boolean' ||
    !Array.isArray(photos) ||
    photos.length < 1 ||
    photos.length > 4
  ) {
    return null;
  }
  const validatedPhotos: Photo[] = [];
  for (const photo of photos) {
    if (!isRecord(photo)) {
      return null;
    }
    const { ref, caption, focusX, focusY } = photo;
    const validRef =
      typeof ref === 'number'
        ? Number.isSafeInteger(ref) && ref > 0
        : isText(ref, 255) && !!ref.trim();
    if (!validRef || (typeof ref !== 'string' && typeof ref !== 'number') || !isText(caption, 26)) {
      return null;
    }
    const percentage = (position: unknown): position is number =>
      typeof position === 'number' && Number.isFinite(position) && position >= 0 && position <= 100;
    if (
      (focusX !== undefined && !percentage(focusX)) ||
      (focusY !== undefined && !percentage(focusY))
    ) {
      return null;
    }
    validatedPhotos.push({ ref, caption, focusX, focusY });
  }
  return {
    version,
    orientation,
    theme,
    layout,
    title,
    subtitle,
    photos: validatedPhotos,
    noteTitle,
    note,
    handwriting,
  };
}

const photoTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);

function designBranch(messages: TMessage[], messageId?: string): TMessage[] {
  const byId = new Map(messages.map((message) => [message.messageId, message]));
  const branch: TMessage[] = [];
  const seen = new Set<string>();
  for (let id = messageId; id && !seen.has(id); id = byId.get(id)?.parentMessageId ?? undefined) {
    seen.add(id);
    const message = byId.get(id);
    if (message) {
      branch.push(message);
    }
  }
  return branch.reverse();
}

export function resolveDesignPhotos(
  design: Design,
  messages: TMessage[],
  messageId?: string,
): Partial<TFile>[] | null {
  const uploaded = new Map<string, Partial<TFile>>();
  for (const message of designBranch(messages, messageId)) {
    if (!message.isCreatedByUser) {
      continue;
    }
    for (const file of message.files ?? []) {
      if (file.file_id && file.type && photoTypes.has(file.type)) {
        uploaded.set(file.file_id, file);
      }
    }
  }
  const files = [...uploaded.values()];
  const selected: Partial<TFile>[] = [];
  for (const { ref } of design.photos) {
    const matches = typeof ref === 'string' ? files.filter((file) => file.filename === ref) : [];
    const file =
      typeof ref === 'number' ? files[ref - 1] : matches.length === 1 ? matches[0] : null;
    if (!file) {
      return null;
    }
    selected.push(file);
  }
  return selected;
}

const revisableFields = new Set([
  'title',
  'subtitle',
  'orientation',
  'theme',
  'layout',
  'noteTitle',
  'note',
  'handwriting',
  'photos',
]);

export function readDesignChanges(source: string): Record<string, unknown> | null {
  if (source.length > 12_000) {
    return null;
  }
  try {
    const value: unknown = JSON.parse(source);
    if (!isRecord(value) || value.version !== 1 || !isRecord(value.changes)) {
      return null;
    }
    const changes = Object.keys(value.changes);
    return changes.length > 0 && changes.every((key) => revisableFields.has(key))
      ? value.changes
      : null;
  } catch {
    return null;
  }
}

export function resolveOfficeDesign(
  source: string,
  identifier: string | undefined,
  messages: TMessage[],
  messageId: string | undefined,
  getMessageText: (message: TMessage) => string,
): Design | null {
  const original = parseOfficeDesign(source);
  if (original) {
    return original;
  }
  const changes = readDesignChanges(source);
  if (!changes || !identifier) {
    return null;
  }
  let design: Design | null = null;
  for (const message of designBranch(messages, messageId)) {
    if (message.isCreatedByUser || message.messageId === messageId) {
      continue;
    }
    const blocks = getMessageText(message).matchAll(
      /^:::artifact\{([^\r\n]*)\}\r?\n(`{3,})json[ \t]*\r?\n([\s\S]*?)\r?\n\2[ \t]*\r?\n:::[ \t]*$/gm,
    );
    for (const [, attributes, , content] of blocks) {
      if (
        !attributes.includes('type="application/vnd.starbridge.a4-design"') ||
        /(?:^|\s)identifier="([^"]+)"/.exec(attributes)?.[1] !== identifier
      ) {
        continue;
      }
      const earlier = readDesignChanges(content);
      const next =
        earlier && design
          ? parseOfficeDesign(JSON.stringify({ ...design, ...earlier }))
          : parseOfficeDesign(content);
      design = next ?? design;
    }
  }
  return design && parseOfficeDesign(JSON.stringify({ ...design, ...changes }));
}

export async function readDesignPhoto(blob: Blob): Promise<string> {
  if (blob.size <= 0 || blob.size > 6 * 1024 * 1024) {
    throw new Error('Photo size');
  }
  const header = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  const ascii = (start: number, end: number) => String.fromCharCode(...header.slice(start, end));
  let type = '';
  if (header[0] === 255 && header[1] === 216 && header[2] === 255) {
    type = 'image/jpeg';
  } else if (header[0] === 137 && ascii(1, 4) === 'PNG') {
    type = 'image/png';
  } else if (ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') {
    type = 'image/webp';
  }
  if (!type) {
    throw new Error('Photo format');
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => (typeof reader.result === 'string' ? resolve(reader.result) : reject());
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob.slice(0, blob.size, type));
  });
}

function escapeText(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    };
    return entities[char];
  });
}

export function designPageSize(design: Design): { width: number; height: number } {
  return design.orientation === 'landscape'
    ? { width: 297, height: 210 }
    : { width: 210, height: 297 };
}

export function renderOfficeDesign(design: Design, photos: string[]): string {
  const { width, height } = designPageSize(design);
  const [ink, accent, paper] = themes[design.theme];
  const figures = design.photos
    .map(({ caption, focusX, focusY }, index) => {
      const image = photos[index] ?? '';
      if (!/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(image)) {
        throw new Error('Invalid photo data');
      }
      return `<figure><img src="${image}" alt="${escapeText(caption)}" style="object-position:${focusX ?? 50}% ${focusY ?? 50}%"><figcaption data-fit>${escapeText(caption)}</figcaption></figure>`;
    })
    .join('');
  const sections = `<div class="photos count-${design.photos.length}">${figures}</div>
<aside><h2 data-fit>${escapeText(design.noteTitle)}</h2><p data-fit>${escapeText(design.note)}</p>${design.handwriting ? '<div class="handwriting"></div>' : ''}</aside>`;
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${escapeText(design.title)}</title><style>
@page { size: ${width}mm ${height}mm; margin: 0; }
* { box-sizing: border-box; }
html,body { margin: 0; color-scheme: light; }
main { width: ${width}mm; height: ${height}mm; padding: 10mm;
  display: grid; grid-template-rows: auto minmax(0,1fr); gap: 5mm;
  color: ${ink}; background: ${paper}; font-family: "Noto Sans CJK SC","Microsoft YaHei","PingFang SC",sans-serif;
  print-color-adjust: exact; -webkit-print-color-adjust: exact; }
header { border-top: 2.5mm solid ${ink}; padding: 4mm 0 2mm; border-bottom: .4mm solid ${ink}; }
h1 { font-size: 9mm; line-height: 1.3; margin: 0 0 1mm; letter-spacing: .02em; }
header p { font-size: 3.8mm; margin: 0; }
.content { display: grid; min-height: 0; gap: 5mm; }
.content.feature { grid-template-columns: minmax(0,2fr) minmax(0,1fr); }
.content.gallery { grid-template-rows: minmax(0,3fr) minmax(0,2fr); }
.photos { min-height: 0; display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); grid-auto-rows: minmax(0,1fr); gap: 3mm; }
.photos.count-1 { grid-template-columns: minmax(0,1fr); }
.feature .photos:not(.count-1) { grid-template-rows: minmax(0,2fr) minmax(0,1fr); }
.feature .photos.count-4 { grid-template-columns: repeat(3,minmax(0,1fr)); }
.feature .photos figure:first-child, .feature .photos.count-2 figure:last-child { grid-column: 1 / -1; }
figure { margin: 0; min-width: 0; min-height: 0; display: grid;
  grid-template-rows: minmax(0,1fr) auto; background: ${accent}; padding: 1.4mm; }
figure img { width: 100%; height: 100%; min-height: 0; object-fit: cover; display: block; }
figcaption { text-align: center; font-size: 3.3mm; min-height: 4.8mm; padding: 1mm .6mm 0; }
aside { min-height: 0; padding: 4mm; border: .35mm solid ${ink}; display: flex; flex-direction: column; }
h2 { font-size: 5mm; margin: 0 0 1.5mm; }
aside p { font-size: 3.7mm; line-height: 1.45; margin: 0; overflow-wrap: anywhere; flex: 0 1 auto; }
.handwriting { flex: 1; min-height: 12mm; margin-top: 3mm; border-top: .3mm dashed ${ink};
  background: repeating-linear-gradient(to bottom,transparent 0 7mm,${accent} 7mm 7.3mm); }
[data-fit] { overflow-wrap:anywhere; }
@media screen { main { zoom: var(--preview-scale,1); } }
</style></head><body><main><header><h1 data-fit>${escapeText(design.title)}</h1>
<p data-fit>${escapeText(design.subtitle)}</p></header>
<div class="content ${design.layout}">${sections}</div></main></body></html>`;
}
