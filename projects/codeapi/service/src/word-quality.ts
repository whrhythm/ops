import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import type { IncomingMessage } from 'node:http';

export interface WordQualityReport {
  version: 'word-quality-v1';
  status: 'checked' | 'repaired';
  source_sha256: string;
  output_sha256: string;
  repairs: string[];
  warnings: string[];
  counts: Record<string, number>;
  render_verified: false;
  source_facts_verified: false;
}
type QualityResult = { ok: true; body: Buffer; report: WordQualityReport } |
  { ok: false; status: 422 | 503; code: string };
let active = false;
const maxBytes = 16 * 1024 * 1024;

/** Called only after the existing execution grant was verified. Never shells out. */
async function checkWordOutput(source: Buffer, repair: boolean): Promise<QualityResult> {
  if (!source.length || source.length > maxBytes) return { ok: false, status: 422, code: 'file_limit' };
    return await new Promise<QualityResult>(resolve => {
      const child = execFile('/usr/bin/python3', ['-I', '/app/word_quality.py', ...(repair ? ['--repair'] : [])],
        { encoding: 'buffer', timeout: 5000, maxBuffer: maxBytes + 16384, env: { LANG: 'C.UTF-8' }, windowsHide: true },
        (error, stdout, stderr) => {
          try {
            const report = JSON.parse(stderr.toString('utf8'));
            if (error) {
              const code = report.status === 'rejected' && typeof report.code === 'string' && /^[a-z_]{1,80}$/.test(report.code)
                ? report.code : 'word_quality_unavailable';
              resolve({ ok: false, status: code === 'word_quality_unavailable' ? 503 : 422, code });
              return;
            }
            const hash = (b: Buffer) => createHash('sha256').update(b).digest('hex');
            if (report.version !== 'word-quality-v1' || !['checked','repaired'].includes(report.status) ||
                report.source_sha256 !== hash(source) || report.output_sha256 !== hash(stdout) ||
                !Array.isArray(report.repairs) || !Array.isArray(report.warnings) ||
                report.render_verified !== false || report.source_facts_verified !== false ||
                stdout.length > maxBytes || !stdout.length) throw Error('invalid_quality_result');
            resolve({ ok: true, body: stdout, report });
          } catch { resolve({ ok: false, status: 503, code: 'word_quality_unavailable' }); }
        });
      child.stdin?.on('error', () => undefined);
      child.stdin?.end(source);
    });
}

export async function checkWordUpload(req: IncomingMessage, expected: number, repair: boolean): Promise<QualityResult> {
  if (active) return { ok: false, status: 503, code: 'word_quality_busy' };
  active = true;
  try {
    const source = await readWordUpload(req, expected);
    if (!source) return { ok: false, status: 422, code: 'invalid_upload_length' };
    return await checkWordOutput(source, repair);
  } finally { active = false; }
}

export async function readWordUpload(req: IncomingMessage, expected: number): Promise<Buffer | null> {
  const chunks: Buffer[] = [];
  let size = 0;
  const timer = setTimeout(() => req.destroy(), 5000);
  try { for await (const part of req) {
    const chunk = Buffer.isBuffer(part) ? part : Buffer.from(part);
    size += chunk.length;
    if (size > expected || size > maxBytes) return null;
    chunks.push(chunk);
  }
    return size === expected ? Buffer.concat(chunks) : null;
  } catch { return null; } finally { clearTimeout(timer); }
}
