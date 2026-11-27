import type { RequestHandler } from 'express';
import type { ManagedUsageDependencies } from './managedUsage';
import type { ServerRequest } from '~/types';
import { managedUsageEnabled } from './managedUsage';

export function officeTaskEndpoint(base: string | undefined, suffix: string): URL {
  if (!managedUsageEnabled(base)) throw new Error('invalid_endpoint');
  const url = new URL(base!);
  if (!url.pathname.endsWith('/account-usage')) throw new Error('invalid_endpoint');
  url.pathname = url.pathname.replace(/account-usage$/, `tasks${suffix}`);
  url.search = '';
  return url;
}

export async function boundedOfficeBody(response: Response): Promise<Buffer> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('response_missing');
  const chunks: Buffer[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 16 * 1024 * 1024) throw new Error('response_limit');
      chunks.push(Buffer.from(value));
    }
  } finally {
    await reader.cancel();
  }
  return Buffer.concat(chunks);
}

/** Fixed backend route and freshly minted identity; browser cannot select another owner. */
export function createOfficeTasksHandler(deps: ManagedUsageDependencies): RequestHandler {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    if (
      !(req as ServerRequest).config?.endpoints?.agents?.durableOfficeTasks ||
      !managedUsageEnabled(deps.url)
    ) {
      res.status(404).json({ code: 'office_tasks_disabled' });
      return;
    }
    const id = req.params.taskId;
    const cancel = req.method === 'POST';
    const conversation = req.query.conversation_id;
    if (
      (id && !/^[a-f0-9-]{36}$/.test(id)) ||
      (cancel && !id) ||
      (!id && (typeof conversation !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(conversation)))
    ) {
      res.status(400).json({ code: 'office_task_invalid' });
      return;
    }
    try {
      const url = officeTaskEndpoint(deps.url, id ? `/${id}${cancel ? '/cancel' : ''}` : '');
      if (!id) url.searchParams.set('conversation_id', String(conversation));
      const headers = await deps.headers(req);
      if (!headers.Authorization) throw new Error('identity_unavailable');
      const response = await deps.fetch(url, {
        headers,
        method: cancel ? 'POST' : 'GET',
        redirect: 'error',
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) {
        res
          .status([403, 404].includes(response.status) ? response.status : 503)
          .json({ code: 'office_tasks_unavailable' });
        return;
      }
      res.json(JSON.parse((await boundedOfficeBody(response)).toString('utf8')));
    } catch {
      res.status(503).json({ code: 'office_tasks_unavailable' });
    }
  };
}
