import type { Request, Response } from 'express';
import { createEnterpriseHandler } from './enterprise';

it('uses the authenticated subject and does not forward browser headers or upstream diagnostics', async () => {
  const fetch = jest.fn().mockResolvedValue(new Response('private diagnostics', { status: 404 }));
  const handler = createEnterpriseHandler({
    url: 'https://platform.test/internal/v1/workspace',
    issuer: 'https://identity.test',
    serviceToken: 'fixture-token',
    fetch,
  });
  const req = {
    user: { provider: 'openid', openidId: 'owner', openidIssuer: 'https://identity.test/' },
    method: 'POST',
    params: { operation: 'activate' },
    headers: { 'X-CodeChat-Subject': 'another-account' },
    body: { code: 'fixture' },
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn() };
  res.status.mockReturnValue(res);

  await handler(req, res as unknown as Response, jest.fn());
  expect(fetch.mock.calls[0][0].href).toBe(
    'https://platform.test/internal/v1/workspace/enterprise/activate',
  );
  expect(fetch.mock.calls[0][1].headers['X-CodeChat-Subject']).toBe('owner');
  expect(res.json).toHaveBeenCalledWith({ code: 'enterprise_code_unavailable' });

  fetch.mockClear();
  req.params.operation = 'constructor';
  await handler(req, res as unknown as Response, jest.fn());
  expect(fetch).not.toHaveBeenCalled();
  req.params.operation = 'activate';
  Object.assign(req.user!, { openidIssuer: 'https://other.test' });
  await handler(req, res as unknown as Response, jest.fn());
  expect(res.status).toHaveBeenLastCalledWith(403);
  expect(fetch).not.toHaveBeenCalled();
});
