import { wordSectionReviewEnabled } from './wordReviewAudience';
import type { RequestHandler } from 'express';
import type { IUser } from '@librechat/data-schemas';
import type { ManagedUsageDependencies } from './managedUsage';
import type { ServerRequest } from '~/types';
import { createHash } from 'node:crypto';
import { boundedOfficeBody, officeTaskEndpoint } from './officeTasks';
import { personalVersions, versionRoot, VersionError } from './personalVersions';

export interface PersonalFilesDependencies {
  url?: string;
  issuer?: string;
  serviceToken?: string;
  fetch: (input: URL, init: RequestInit) => Promise<Response>;
  officeTasks?: ManagedUsageDependencies;
  scanUrl?: string;
  scanToken?: string;
  scanSubject?: string;
  scanEnabled?: boolean;
}

export function scanAvailableTo(user: Partial<IUser> | undefined, issuer?: string, subject?: string): boolean {
  return !!issuer && !!subject && user?.provider === 'openid' && !!user.openidId &&
    (subject === 'all' || user.openidId === subject) &&
    user.openidIssuer?.replace(/\/$/, '') === issuer.replace(/\/$/, '');
}

const limit = 16 * 1024 * 1024;
const methods: Record<string, readonly string[]> = {
  directories: ['GET', 'POST'],
  usage: ['GET'],
  file: ['GET', 'PUT'],
  revisions: ['GET', 'POST'],
  'task-revision': ['POST'],
  'scan': ['GET', 'POST'],
  'scan-page': ['GET'],
  'scan-word': ['GET'],
};

export function personalFilesEnabled(url?: string): boolean {
  try {
    const target = new URL(url ?? '');
    return (
      target.protocol === 'https:' &&
      !target.username &&
      !target.password &&
      !target.search &&
      !target.hash
    );
  } catch {
    return false;
  }
}

/** Deployment-owned file bridge: no caller-controlled owner, host or overwrite. */
export function createPersonalFilesHandler(deps: PersonalFilesDependencies): RequestHandler {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const user = req.user as IUser | undefined;
    if (!personalFilesEnabled(deps.url) || !deps.serviceToken || !deps.issuer) {
      res.status(404).json({ code: 'personal_files_disabled' });
      return;
    }
    if (
      user?.provider !== 'openid' ||
      !user.openidId ||
      user.openidIssuer?.replace(/\/$/, '') !== deps.issuer.replace(/\/$/, '')
    ) {
      res.status(403).json({ code: 'personal_files_identity' });
      return;
    }
    const operation = String(req.params.operation ?? '');
    if (operation === 'review-status' && req.method === 'GET') {
      const audience = process.env.STARBRIDGE_WORD_REVIEW_AUDIENCE;
      res.json({ enabled: audience === 'all' || (!!audience && user.email === audience),
        sections: wordSectionReviewEnabled(user) });
      return;
    }
    const path = req.query.path ?? '';
    const previewWord = req.query.preview === 'word';
    if (
      !Object.prototype.hasOwnProperty.call(methods, operation) ||
      !methods[operation]?.includes(req.method) ||
      typeof path !== 'string' ||
      (req.query.preview !== undefined &&
        (!previewWord || req.method !== 'GET' || !/\.docx$/i.test(path) ||
          !(operation === 'file' || (operation === 'revisions' && req.query.version)))) ||
      path.length > 4096 ||
      path.includes('\\') ||
      [...path].some((char) => char.charCodeAt(0) < 32) ||
      path.startsWith('/') ||
      path.split('/')[0] === versionRoot ||
      (path && path.split('/').some((part) => !part || part === '.' || part === '..')) ||
      ((operation === 'file' || operation === 'revisions' || req.method === 'POST') && !path)
    ) {
      res.status(400).json({ code: 'personal_files_invalid' });
      return;
    }
    if (
      (req.method === 'PUT' || (operation === 'revisions' && req.method === 'POST')) &&
      (!Buffer.isBuffer(req.body) || req.body.length > limit)
    ) {
      res.status(413).json({ code: 'personal_files_too_large' });
      return;
    }
    try {
      const sendWordPreview = async (source: Buffer) => {
        const { extractCodeArtifactText } = await import('../files/code/extract');
        const html = await extractCodeArtifactText(source, path, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'document');
        if (!html) throw new VersionError(422);
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.status(200).json({ html, sha256: createHash('sha256').update(source).digest('hex') });
      };
      if (operation === 'scan' || operation === 'scan-page' || operation === 'scan-word') {
        if (!(deps.scanEnabled || (req as ServerRequest).config?.endpoints?.agents?.structuredOcr) ||
            !personalFilesEnabled(deps.scanUrl) || !deps.scanToken ||
            !scanAvailableTo(user, deps.issuer, deps.scanSubject)) throw new VersionError(404);
        const { conversation, digest, page } = req.query;
        if (typeof conversation !== 'string' || !/^[a-f0-9-]{36}$/.test(conversation)) {
          throw new VersionError(400);
        }
        let suffix = '';
        if (operation !== 'scan') {
          if (typeof digest !== 'string' || !/^[a-f0-9]{64}$/.test(digest)) throw new VersionError(400);
          if (operation === 'scan-page' && (typeof page !== 'string' || !/^(?:[1-9]|1\d|20)$/.test(page))) {
            throw new VersionError(400);
          }
          suffix = `/${digest}/${operation === 'scan-word' ? 'word' : `pages/${page}`}`;
        }
        let input: Buffer | undefined;
        if (req.method === 'POST') {
          if (!/\.(?:pdf|png|jpe?g)$/i.test(path)) throw new VersionError(400);
          const source = new URL(`${deps.url!.replace(/\/$/, '')}/file`);
          source.searchParams.set('path', path);
          const original = await deps.fetch(source, {
            headers: { 'X-CodeChat-Service-Token': deps.serviceToken, 'X-CodeChat-Subject': user.openidId },
            redirect: 'error', signal: AbortSignal.timeout(20000),
          });
          if (!original.ok) throw new VersionError(original.status === 404 ? 404 : 503);
          input = await boundedOfficeBody(original);
          if (input.length > 10 * 1024 * 1024) throw new VersionError(413);
        }
        const owner = createHash('sha256').update(JSON.stringify([deps.issuer, user.openidId])).digest('hex');
        const scanUrl = new URL(`${deps.scanUrl!.replace(/\/$/, '')}/scans/${conversation}${suffix}`);
        if (req.method === 'POST' && req.query.pages !== undefined) {
          if (typeof req.query.pages !== 'string' || req.query.pages.length > 100 ||
              !/^[\d,-]+$/.test(req.query.pages)) throw new VersionError(400);
          scanUrl.searchParams.set('pages', req.query.pages);
        }
        const response = await deps.fetch(scanUrl, {
          method: req.method,
          headers: { Authorization: `Bearer ${deps.scanToken}`, 'X-Scan-Owner': owner },
          ...(input ? { body: new Uint8Array(input) } : {}),
          redirect: 'error', signal: AbortSignal.timeout(20000),
        });
        if (!response.ok) {
          await response.body?.cancel();
          throw new VersionError([400, 403, 404, 409, 410, 413, 415, 422, 429].includes(response.status) ? response.status : 503);
        }
        const body = await boundedOfficeBody(response);
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.contentType(operation === 'scan-page' ? 'image/png' : operation === 'scan-word'
          ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' : 'application/json');
        res.status(200).send(body);
        return;
      }
      if (operation === 'task-revision') {
        const { taskId, fileId, version: expectedVersion } = req.query;
        if (
          !deps.officeTasks ||
          !(req as ServerRequest).config?.endpoints?.agents?.durableOfficeTasks
        ) {
          throw new VersionError(404);
        }
        if (
          typeof taskId !== 'string' ||
          !/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(taskId) ||
          typeof fileId !== 'string' ||
          !/^[\w-]{1,128}$/.test(fileId) ||
          (expectedVersion !== undefined &&
            (typeof expectedVersion !== 'string' || !/^[1-9]\d{0,5}$/.test(expectedVersion)))
        ) {
          throw new VersionError(400);
        }
        const revisions = personalVersions(deps, user.openidId, path);
        const history = await revisions.list();
        // The durable snapshot is authoritative even after temporary output expires.
        const duplicate = history.find(
          (entry) => entry.source?.taskId === taskId && entry.source?.fileId === fileId,
        );
        if (duplicate) {
          res.status(200).json(duplicate);
          return;
        }
        const version = Math.max(1, ...history.map((entry) => entry.version)) + 1;
        if (expectedVersion !== undefined && Number(expectedVersion) !== version) {
          throw new VersionError(409);
        }
        const headers = await deps.officeTasks.headers(req);
        if (!headers.Authorization) throw new VersionError(403);
        const output = await deps.officeTasks.fetch(
          officeTaskEndpoint(deps.officeTasks.url, `/${taskId}/outputs/${fileId}`),
          { headers, redirect: 'error', signal: AbortSignal.timeout(40000) },
        );
        if (!output.ok) {
          if (output.body) await output.body.cancel();
          throw new VersionError(
            [403, 404, 410, 413].includes(output.status) ? output.status : 503,
          );
        }
        const body = await boundedOfficeBody(output);
        const requestId = createHash('sha256')
          .update(JSON.stringify([taskId, fileId, path]))
          .digest('hex');
        const saved = await revisions.save(version, requestId, body, { taskId, fileId });
        res.status(201).json(saved);
        return;
      }
      if (operation === 'revisions') {
        const raw = req.query.version;
        const version =
          typeof raw === 'string' && /^[1-9]\d{0,5}$/.test(raw) ? Number(raw) : undefined;
        const requestId = req.query.requestId;
        if (
          (raw !== undefined && !version) ||
          (req.method === 'POST' &&
            (!version || typeof requestId !== 'string' || !/^[a-f0-9-]{36}$/.test(requestId)))
        ) {
          res.status(400).json({ code: 'personal_files_invalid' });
          return;
        }
        const revisions = personalVersions(deps, user.openidId, path);
        if (req.method === 'POST') {
          const result = await revisions.save(version!, String(requestId), req.body);
          res.status(201).json(result);
        } else if (version) {
          const source = await revisions.download(version);
          if (previewWord) return await sendWordPreview(source);
          res.setHeader('X-Content-Type-Options', 'nosniff');
          res.setHeader('Content-Type', 'application/octet-stream');
          res.status(200).send(source);
        } else {
          res.status(200).json({ revisions: await revisions.list() });
        }
        return;
      }
      const url = new URL(`${deps.url!.replace(/\/$/, '')}/${operation}`);
      url.searchParams.set('path', path);
      const result = await deps.fetch(url, {
        method: req.method,
        headers: {
          'X-CodeChat-Service-Token': deps.serviceToken,
          'X-CodeChat-Subject': user.openidId,
          'Content-Type': 'application/octet-stream',
        },
        ...(req.method === 'PUT' ? { body: new Uint8Array(req.body) } : {}),
        redirect: 'error',
        signal: AbortSignal.timeout(40000),
      });
      if (!result.ok) {
        if (result.status === 409 && operation === 'file' && req.method === 'PUT') {
          const existing = await deps.fetch(url, {
            method: 'GET',
            headers: {
              'X-CodeChat-Service-Token': deps.serviceToken,
              'X-CodeChat-Subject': user.openidId,
            },
            redirect: 'error',
            signal: AbortSignal.timeout(40000),
          });
          if (!existing.ok) throw new Error('save_status_unavailable');
          const reader = existing.body?.getReader();
          if (!reader) throw new Error('save_status_unavailable');
          let offset = 0;
          let same = true;
          try {
            for (;;) {
              const chunk = await reader.read();
              if (chunk.done) break;
              if (
                offset + chunk.value.length > req.body.length ||
                !Buffer.from(chunk.value).equals(
                  req.body.subarray(offset, offset + chunk.value.length),
                )
              ) {
                same = false;
                break;
              }
              offset += chunk.value.length;
            }
          } finally {
            await reader.cancel();
          }
          if (same && offset === req.body.length) {
            res.status(200).json({ saved: true, recovered: true });
            return;
          }
        }
        const status = [400, 403, 404, 409, 413, 507].includes(result.status) ? result.status : 503;
        res.status(status).json({ code: `personal_files_${status}` });
        return;
      }
      const reader = result.body?.getReader();
      if (!reader) throw new Error('empty_response');
      const chunks: Uint8Array[] = [];
      let bytes = 0;
      for (;;) {
        const next = await reader.read();
        if (next.done) break;
        bytes += next.value.length;
        if (bytes > limit) {
          await reader.cancel();
          throw new Error('response_limit');
        }
        chunks.push(next.value);
      }
      res.setHeader('X-Content-Type-Options', 'nosniff');
      res.setHeader(
        'Content-Type',
        operation === 'file' && req.method === 'GET'
          ? 'application/octet-stream'
          : 'application/json',
      );
      const content = Buffer.concat(chunks);
      if (previewWord) {
        res.setHeader('Content-Type', 'application/json');
        return await sendWordPreview(content);
      }
      if (operation === 'directories' && req.method === 'GET' && !path) {
        const listing = JSON.parse(content.toString('utf8'));
        listing.entries = listing.entries.filter(
          (entry: { name: string }) => entry.name !== versionRoot,
        );
        res.status(result.status).json(listing);
      } else res.status(result.status).send(content);
    } catch (error) {
      const status = error instanceof VersionError ? error.status : 503;
      res.status(status).json({ code: `personal_files_${status}` });
    }
  };
}
