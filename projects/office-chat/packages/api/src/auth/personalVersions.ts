import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { PersonalFilesDependencies } from './personalFiles';

export const versionRoot = '.starbridge-versions';
const marker = 'STARBRIDGE-VERSION-1\n';
const maxBytes = 16 * 1024 * 1024; // Same transfer limit as the existing personal file service.
const headerSchema = z.object({
  version: z.number().int().positive(),
  requestId: z.string(),
  path: z.string(),
  size: z.number().int().nonnegative(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  savedAt: z.string().datetime(),
  source: z.object({ taskId: z.string().uuid(), fileId: z.string().regex(/^[\w-]+$/) }).optional(),
});
type Header = z.infer<typeof headerSchema>;
type Source = NonNullable<Header['source']>;
export class VersionError extends Error {
  constructor(public readonly status: number) {
    super('personal_version_error');
  }
}
const digest = (body: Uint8Array | string) => createHash('sha256').update(body).digest('hex');

/** Immutable, atomic snapshot envelopes live on the owner's existing volume.
 * One create-only object contains metadata AND bytes: no partial metadata commit or orphan blob.
 * Reading a history header cancels the stream before downloading the whole workbook.
 */
export function personalVersions(deps: PersonalFilesDependencies, subject: string, path: string) {
  const folder = `${versionRoot}/${digest(path)}`;
  const target = (version: number) => `${folder}/${version}.snapshot`;
  const request = async (
    operation: string,
    method: string,
    storagePath: string,
    body?: Uint8Array,
  ) => {
    const url = new URL(`${deps.url!.replace(/\/$/, '')}/${operation}`);
    url.searchParams.set('path', storagePath);
    const response = await deps.fetch(url, {
      method,
      headers: {
        'X-CodeChat-Service-Token': deps.serviceToken!,
        'X-CodeChat-Subject': subject,
        'Content-Type': 'application/octet-stream',
      },
      ...(body ? { body: new Uint8Array(body) } : {}),
      redirect: 'error',
      signal: AbortSignal.timeout(40000),
    });
    if (!response.ok)
      throw new VersionError(
        [400, 403, 404, 409, 413, 507].includes(response.status) ? response.status : 503,
      );
    return response;
  };
  const read = async (storagePath: string, headerOnly = false) => {
    const response = await request('file', 'GET', storagePath);
    const reader = response.body?.getReader();
    if (!reader) throw new VersionError(503);
    let result = Buffer.alloc(0);
    try {
      for (;;) {
        const part = await reader.read();
        if (part.done) break;
        result = Buffer.concat([result, part.value]);
        if (result.length > maxBytes) throw new VersionError(413);
        if (headerOnly && result.indexOf('\n', marker.length) >= 0) break;
      }
    } finally {
      await reader.cancel();
    }
    return result;
  };
  const unpack = (packet: Buffer, version: number, full: boolean) => {
    const end = packet.indexOf('\n', marker.length);
    if (!packet.subarray(0, marker.length).equals(Buffer.from(marker)) || end < 0 || end > 8192)
      throw new VersionError(503);
    const header = headerSchema.parse(
      JSON.parse(packet.subarray(marker.length, end).toString('utf8')),
    );
    if (header.path !== path || header.version !== version) throw new VersionError(503);
    const body = packet.subarray(end + 1);
    if (full && (body.length !== header.size || digest(body) !== header.sha256))
      throw new VersionError(503);
    return { header, body };
  };
  const snapshot = async (version: number, requestId: string, body: Buffer, source?: Source) => {
    const header: Header = {
      version,
      requestId,
      path,
      size: body.length,
      sha256: digest(body),
      savedAt: new Date().toISOString(),
      ...(source ? { source } : {}),
    };
    const packet = Buffer.concat([Buffer.from(`${marker}${JSON.stringify(header)}\n`), body]);
    if (packet.length > maxBytes) throw new VersionError(413);
    try {
      await request('file', 'PUT', target(version), packet);
    } catch (error) {
      if (!(error instanceof VersionError) || error.status !== 409) throw error;
      const existing = unpack(await read(target(version)), version, true).header;
      if (
        existing.requestId !== requestId ||
        existing.sha256 !== header.sha256 ||
        existing.source?.taskId !== source?.taskId ||
        existing.source?.fileId !== source?.fileId
      )
        throw new VersionError(409);
      return existing;
    }
    return header;
  };
  const list = async () => {
    let entries: { name: string; kind: string }[];
    try {
      entries = (await (await request('directories', 'GET', folder)).json()).entries;
    } catch (error) {
      if (error instanceof VersionError && error.status === 404) return [];
      throw error;
    }
    if (!Array.isArray(entries)) throw new VersionError(503);
    const numbers = entries
      .filter((e) => e.kind === 'file' && /^[1-9]\d*\.snapshot$/.test(e.name))
      .map((e) => Number(e.name.split('.')[0]))
      .sort((a, b) => b - a);
    const result: Header[] = [];
    // Bounded parallel reads avoid a serial storage round trip for every revision.
    for (let i = 0; i < numbers.length; i += 4) {
      result.push(
        ...(await Promise.all(
          numbers
            .slice(i, i + 4)
            .map(async (v) => unpack(await read(target(v), true), v, false).header),
        )),
      );
    }
    return result;
  };
  return {
    list,
    download: async (version: number) => unpack(await read(target(version)), version, true).body,
    save: async (version: number, requestId: string, body: Buffer, source?: Source) => {
      if (version < 2) throw new VersionError(400);
      const existing = await list();
      const duplicate = existing.find((h) => h.requestId === requestId);
      if (duplicate) {
        if (
          duplicate.version !== version ||
          duplicate.sha256 !== digest(body) ||
          duplicate.source?.taskId !== source?.taskId ||
          duplicate.source?.fileId !== source?.fileId
        )
          throw new VersionError(409);
        return unpack(await read(target(version)), version, true).header;
      }
      if (version !== Math.max(1, ...existing.map((h) => h.version)) + 1)
        throw new VersionError(409);
      if (!existing.length) {
        const original = await read(path);
        for (const dir of [versionRoot, folder]) {
          try {
            await request('directories', 'POST', dir);
          } catch (error) {
            if (!(error instanceof VersionError) || error.status !== 409) throw error;
          }
        }
        await snapshot(1, 'original', original);
      }
      return snapshot(version, requestId, body, source);
    },
  };
}
