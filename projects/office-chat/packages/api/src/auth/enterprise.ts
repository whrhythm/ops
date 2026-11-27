import type { IUser } from '@librechat/data-schemas';
import type { RequestHandler } from 'express';
import type { PersonalFilesDependencies } from './personalFiles';
import { personalFilesEnabled } from './personalFiles';

const operations = {
  overview: 'GET',
  apply: 'POST',
  activate: 'POST',
  setup: 'POST',
} as const;

const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const isId = (value: unknown): value is string => typeof value === 'string' && uuid.test(value);

function collaborationPath(method: string, params: Record<string, unknown>): string | undefined {
  const { operation, enterpriseId, assetId, invitationId, number } = params;
  if (method === 'GET' && operation === 'invitations' && enterpriseId == null)
    return '/invitations';
  if (enterpriseId == null && isId(invitationId) && method === 'POST') {
    return `/invitations/${invitationId}/accept`;
  }
  if (!isId(enterpriseId)) return;
  const root = `/${enterpriseId}`;
  if (isId(invitationId) && method === 'POST') return `${root}/invitations/${invitationId}/revoke`;
  if (isId(assetId)) {
    const versions = `${root}/files/${assetId}/versions`;
    if (number == null && (method === 'GET' || method === 'POST')) return versions;
    if (method === 'GET' && typeof number === 'string' && /^[1-9]\d{0,2}$/.test(number)) {
      return `${versions}/${number}`;
    }
    return;
  }
  if (method === 'GET' && (operation === 'members' || operation === 'files'))
    return `${root}/${operation}`;
  if (method === 'POST' && (operation === 'invite' || operation === 'files'))
    return `${root}/${operation}`;
}

export function createEnterpriseHandler(deps: PersonalFilesDependencies): RequestHandler {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    if (!personalFilesEnabled(deps.url) || !deps.serviceToken || !deps.issuer) {
      res.status(404).json({ code: 'enterprise_unavailable' });
      return;
    }
    const user = req.user as IUser | undefined;
    if (
      user?.provider !== 'openid' ||
      !user.openidId ||
      user.openidIssuer?.replace(/\/$/, '') !== deps.issuer.replace(/\/$/, '')
    ) {
      res.status(403).json({ code: 'enterprise_identity' });
      return;
    }
    const operation = req.params.operation as keyof typeof operations;
    const id = req.params.enterpriseId;
    const isOpening =
      Object.prototype.hasOwnProperty.call(operations, operation) &&
      operations[operation] === req.method &&
      (operation === 'setup' ? isId(id) : id == null);
    const path = isOpening
      ? operation === 'overview'
        ? ''
        : operation === 'setup'
          ? `/${id}/setup`
          : `/${operation}`
      : collaborationPath(req.method, req.params);
    if (path == null) {
      res.status(404).json({ code: 'enterprise_unavailable' });
      return;
    }
    try {
      const upstream = await deps.fetch(
        new URL(`${deps.url!.replace(/\/$/, '')}/enterprise${path}`),
        {
          method: req.method,
          headers: {
            'X-CodeChat-Service-Token': deps.serviceToken,
            'X-CodeChat-Subject': user.openidId,
            'Content-Type': 'application/json',
          },
          ...(req.method === 'POST' ? { body: JSON.stringify(req.body) } : {}),
          redirect: 'error',
          signal: AbortSignal.timeout(isOpening ? 10000 : 18000),
        },
      );
      if (!upstream.ok) {
        const status = [403, 404, 409, 413, 422, 507].includes(upstream.status)
          ? upstream.status
          : 503;
        const failure = await upstream.json().catch(() => ({}));
        res.status(status).json({
          code:
            operation === 'activate' && status === 404
              ? 'enterprise_code_unavailable'
              : typeof failure.detail === 'string' && /^enterprise_[a-z_]+$/.test(failure.detail)
                ? failure.detail
                : `enterprise_${status}`,
        });
        return;
      }
      if (req.params.number != null) {
        const size = Number(upstream.headers.get('content-length'));
        if (!Number.isSafeInteger(size) || size < 1 || size > 1048576)
          throw new Error('invalid file size');
        const bytes = Buffer.from(await upstream.arrayBuffer());
        if (bytes.length !== size) throw new Error('incomplete file');
        res.setHeader('Content-Type', 'application/octet-stream');
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.setHeader('Content-Disposition', 'attachment');
        res.send(bytes);
        return;
      }
      res.json(await upstream.json());
    } catch {
      res.status(503).json({ code: 'enterprise_unavailable' });
    }
  };
}
