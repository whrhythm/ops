import type { RequestHandler } from 'express';
import { managedUsageEnabled } from './managedUsage';

export function createManagedPurchasesHandler(deps: {
  url?: string;
  issuer?: string;
  serviceToken?: string;
  subject?: string;
  fetch: (url: URL, options: RequestInit) => Promise<Response>;
}): RequestHandler {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const user = req.user as {provider?: string; openidId?: string; openidIssuer?: string} | undefined;
    if (!deps.subject || !deps.issuer || !deps.serviceToken || !managedUsageEnabled(deps.url) ||
        user?.provider !== 'openid' || !user.openidId || (deps.subject !== '*' && user.openidId !== deps.subject) ||
        user.openidIssuer?.replace(/\/$/, '') !== deps.issuer?.replace(/\/$/, '')) {
      res.status(req.method === 'GET' ? 200 : 404).json({ enabled: false });
      return;
    }
    const action = req.params.action;
    const order = req.params.orderId;
    const base = '/internal/v1/workspace/capacity';
    const paths: Record<string, string> = { checkout: 'checkout', payment: 'payment', replay: 'replay-payment', retry: 'retry' };
    let pathname = base;
    if (action === 'create' && req.method === 'POST') pathname += '/orders';
    else if (order && /^[0-9a-f-]{36}$/.test(order) && action && paths[action] &&
      ((action === 'payment' && req.method === 'GET') || (action !== 'payment' && req.method === 'POST'))) {
      pathname += '/orders/' + order + '/' + paths[action];
    } else if (action || req.method !== 'GET') {
      res.sendStatus(404);
      return;
    }
    const headers: Record<string, string> = {
      'X-CodeChat-Service-Token': deps.serviceToken, 'X-CodeChat-Subject': user.openidId!,
    };
    let body: string | undefined;
    if (action === 'create') {
      const key = req.get('Idempotency-Key');
      if (!key || !/^[\w-]{1,128}$/.test(key) || typeof req.body?.plan_id !== 'string' ||
          typeof req.body?.plan_version !== 'string') {
        res.sendStatus(400);
        return;
      }
      headers['Idempotency-Key'] = key;
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify({ plan_id: req.body.plan_id, plan_version: req.body.plan_version });
    }
    try {
      const upstream = await deps.fetch(new URL(pathname, deps.url), {
        method: req.method, headers, body, redirect: 'error', signal: AbortSignal.timeout(15000),
      });
      if (!upstream.ok) {
        res.status([400, 403, 404, 409, 429, 503].includes(upstream.status) ? upstream.status : 503)
          .json({ code: 'purchase_unavailable' });
        return;
      }
      const data = await upstream.json();
      // A server-loopback checkout URL cannot be used by the person's browser.
      if ('checkout_url' in data) data.checkout_url = null;
      res.json({ ...data, enabled: true });
    } catch {
      res.status(503).json({ code: 'purchase_unavailable' });
    }
  };
}
