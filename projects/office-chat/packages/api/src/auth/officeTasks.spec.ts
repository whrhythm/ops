import type { Request, Response } from 'express';
import { createOfficeTasksHandler } from './officeTasks';

it('uses fixed backend and minted identity without forwarding owner or URL selectors', async () => {
  const fetch = jest.fn(async () => new Response(JSON.stringify({ tasks: [] })));
  const handler = createOfficeTasksHandler({
    url: 'https://platform.test/internal/v1/codeapi/account-usage',
    headers: async () => ({ Authorization: 'Bearer server-minted' }),
    fetch,
  });
  const req = {
    config: { endpoints: { agents: { durableOfficeTasks: true } } },
    params: {},
    query: { conversation_id: 'conversation-1', subject: 'victim', url: 'http://attacker' },
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn() };
  res.status.mockReturnValue(res);
  await handler(req, res as unknown as Response, jest.fn());
  const [url, init] = fetch.mock.calls[0] as unknown as [URL, RequestInit];
  expect(url.href).toBe(
    'https://platform.test/internal/v1/codeapi/tasks?conversation_id=conversation-1',
  );
  expect(init.headers).toEqual({ Authorization: 'Bearer server-minted' });
  expect(res.json).toHaveBeenCalledWith({ tasks: [] });
});
