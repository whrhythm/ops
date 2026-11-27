import type { RequestHandler } from 'express';
import type { IUser } from '@librechat/data-schemas';

/** Deployment-owned provisioning; never accepts an endpoint or identity from the browser. */
export interface ManagedModelDependencies {
  url?: string;
  serviceToken?: string;
  endpoint?: string;
  timeoutMs?: number;
  fetch: (input: URL, init: RequestInit) => Promise<Response>;
  updateUserKey: (input: { userId: string; name: string; value: string }) => Promise<unknown>;
}

const normalizeIssuer = (value: string) => value.replace(/\/$/, '');

export function createManagedModelProvisioning(deps: ManagedModelDependencies): RequestHandler {
  return async (req, res, next) => {
    if (!deps.url) {
      next();
      return;
    }
    const user = req.user as (IUser & { tokenset?: { access_token?: string } }) | undefined;
    try {
      const url = new URL(deps.url);
      if (
        url.protocol !== 'https:' ||
        url.username ||
        url.password ||
        !deps.serviceToken ||
        user?.provider !== 'openid' ||
        !user.openidId ||
        !user.openidIssuer ||
        !user.tokenset?.access_token ||
        !user._id
      ) {
        throw new Error('managed_model_setup_unavailable');
      }
      const response = await deps.fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${user.tokenset.access_token}`,
          'X-CodeChat-Service-Token': deps.serviceToken,
        },
        redirect: 'error',
        signal: AbortSignal.timeout(deps.timeoutMs ?? 30000),
      });
      if (!response.ok) {
        throw new Error('managed_model_setup_unavailable');
      }
      const result = await response.json();
      if (
        result?.subject !== user.openidId ||
        typeof result?.issuer !== 'string' ||
        normalizeIssuer(result.issuer) !== normalizeIssuer(user.openidIssuer) ||
        typeof result?.key !== 'string' ||
        !result.key.startsWith('sk-')
      ) {
        throw new Error('managed_model_identity_mismatch');
      }
      // Existing persistence encrypts this value. Provisioning reuses the platform identity/key.
      await deps.updateUserKey({
        userId: user._id.toString(),
        name: deps.endpoint ?? 'LiteLLM',
        value: JSON.stringify({ apiKey: result.key }),
      });
      next();
    } catch {
      res.setHeader('Cache-Control', 'no-store');
      res.status(503).json({ code: 'managed_model_setup_unavailable' });
    }
  };
}

export function createManagedCredentialGuard(config: {
  enabled: boolean;
  endpoint?: string;
}): RequestHandler {
  return (req, res, next) => {
    const endpoint = config.endpoint ?? 'LiteLLM';
    const managedWrite = req.method === 'PUT' && req.body?.name === endpoint;
    const managedDelete =
      req.method === 'DELETE' && (req.params.name === endpoint || req.query.all === 'true');
    if (config.enabled && (managedWrite || managedDelete)) {
      res.status(403).json({ code: 'managed_credential_read_only' });
      return;
    }
    next();
  };
}
