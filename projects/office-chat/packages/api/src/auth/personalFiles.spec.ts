import type { Request, Response } from 'express';
import { createPersonalFilesHandler, scanAvailableTo } from './personalFiles';
import JSZip from 'jszip';
import { createHash } from 'node:crypto';

it('opens document tools only to authenticated users from the configured issuer', () => {
  const user = { provider:'openid', openidId:'owner', openidIssuer:'https://login.test/' };
  expect(scanAvailableTo(user,'https://login.test','all')).toBe(true);
  expect(scanAvailableTo(user,'https://login.test','other')).toBe(false);
  expect(scanAvailableTo({...user,openidId:''},'https://login.test','all')).toBe(false);
  expect(scanAvailableTo({...user,provider:'local'},'https://login.test','all')).toBe(false);
  expect(scanAvailableTo(user,'https://other.test','all')).toBe(false);
});

function setup(ocr = false) {
  const req = {
    user: { provider: 'openid', openidId: 'owner', openidIssuer: 'https://login.test/' },
    method: 'GET',
    params: { operation: 'directories' },
    query: { path: '', subject: 'victim', overwrite: 'true', url: 'http://attacker' },
    body: Buffer.from('synthetic'),
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn(), send: jest.fn(), contentType: jest.fn() };
  res.status.mockReturnValue(res);
  const fetch = jest
    .fn()
    .mockImplementation(async () => new Response('{"entries":[]}', { status: 200 }));
  const handler = createPersonalFilesHandler({
    url: 'https://platform.test/internal/v1/workspace',
    issuer: 'https://login.test',
    serviceToken: 'private-service-token',
    fetch,
    ...(ocr ? { scanUrl: 'https://scan.test', scanToken: 'private-scan-token', scanSubject: 'owner' } : {}),
  });
  return { req, res, fetch, run: () => handler(req, res as unknown as Response, jest.fn()) };
}

it('previews the exact bytes returned by owner-scoped storage', async () => {
  const c = setup();
  const zip = new JSZip();
  zip.file('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>文件原件</w:t></w:r></w:p></w:body></w:document>');
  const bytes = await zip.generateAsync({ type: 'nodebuffer' });
  c.req.params.operation = 'file';
  c.req.query = { path: 'report.docx', preview: 'word' };
  c.fetch.mockResolvedValueOnce(new Response(new Uint8Array(bytes)));
  await c.run();
  expect(c.fetch.mock.calls[0][1].headers['X-CodeChat-Subject']).toBe('owner');
  expect(c.res.json).toHaveBeenCalledWith({
    sha256: createHash('sha256').update(bytes).digest('hex'),
    html: expect.stringContaining('文件原件'),
  });
});

it('requires an explicit version for a historical Word preview', async () => {
  const c = setup();
  c.req.params.operation = 'revisions';
  c.req.query = { path: 'report.docx', preview: 'word' };
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(400);
  expect(c.fetch).not.toHaveBeenCalled();
});

it('reads only the signed-in user’s PDF and posts bytes to the configured scan service', async () => {
  const c = setup(true);
  Object.assign(c.req, { config: { endpoints: { agents: { structuredOcr: true } } } });
  c.req.params.operation = 'scan';
  c.req.method = 'POST';
  c.req.query.path = 'reports/source.pdf';
  c.req.query.pages = '2,4-5';
  c.req.query.conversation = '11111111-1111-4111-8111-111111111111';
  c.fetch.mockResolvedValueOnce(new Response('%PDF-synthetic'));
  await c.run();
  const [source, scan] = c.fetch.mock.calls;
  expect(source[0].searchParams.get('path')).toBe('reports/source.pdf');
  expect(source[1].headers['X-CodeChat-Subject']).toBe('owner');
  expect(scan[0].origin).toBe('https://scan.test');
  expect(scan[0].searchParams.get('pages')).toBe('2,4-5');
  expect(scan[0].pathname).toBe('/scans/11111111-1111-4111-8111-111111111111');
  expect(scan[1].headers['X-Scan-Owner']).toMatch(/^[a-f0-9]{64}$/);
  expect(Buffer.from(scan[1].body).toString()).toBe('%PDF-synthetic');
  expect(c.res.status).toHaveBeenCalledWith(200);
});

it('rejects a scan out of range before calling the worker', async () => {
  const c = setup(true);
  Object.assign(c.req, { config: { endpoints: { agents: { structuredOcr: true } } } });
  c.req.params.operation = 'scan-page';
  c.req.query = { conversation: '11111111-1111-4111-8111-111111111111', digest: 'a'.repeat(64), page: '21' };
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(400);
  expect(c.fetch).not.toHaveBeenCalled();
});

it.each(['scan', 'scan-page', 'scan-word'])('keeps %s private to the enabled account', async operation => {
  const c = setup(true);
  Object.assign(c.req, { user: { provider: 'openid', openidIssuer: 'https://login.test/', openidId: 'other' },
    config: { endpoints: { agents: { structuredOcr: true } } } });
  c.req.params.operation = operation;
  c.req.query = { conversation: '11111111-1111-4111-8111-111111111111', digest: 'a'.repeat(64), page: '1' };
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(404);
  expect(c.fetch).not.toHaveBeenCalled();
});

it('forwards only verified owner and bounded operation/path, ignoring caller selectors', async () => {
  const c = setup();
  await c.run();
  const [url, init] = c.fetch.mock.calls[0];
  expect(url.href).toBe('https://platform.test/internal/v1/workspace/directories?path=');
  expect(init.headers['X-CodeChat-Subject']).toBe('owner');
  expect(init.redirect).toBe('error');
  expect(c.res.json).toHaveBeenCalledWith({ entries: [] });
});
it('rejects mismatched issuers before calling storage', async () => {
  const c = setup();
  Object.assign(c.req.user!, { openidIssuer: 'https://other.test' });
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(403);
  expect(c.fetch).not.toHaveBeenCalled();
});
it.each(['../other', '/root', 'a/../b', 'a\\b', 'a//b', 'a\u0000b'])(
  'rejects path %s',
  async (path) => {
    const c = setup();
    c.req.query.path = path;
    await c.run();
    expect(c.res.status).toHaveBeenCalledWith(400);
    expect(c.fetch).not.toHaveBeenCalled();
  },
);
it.each(['constructor', '__proto__', 'apps'])(
  'rejects unregistered operation %s',
  async (operation) => {
    const c = setup();
    c.req.params.operation = operation;
    await c.run();
    expect(c.res.status).toHaveBeenCalledWith(400);
    expect(c.fetch).not.toHaveBeenCalled();
  },
);

it('does not expose deletion or arbitrary routes', async () => {
  const c = setup();
  c.req.method = 'DELETE';
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(400);
  expect(c.fetch).not.toHaveBeenCalled();
});
it('preserves conflict, never overwrites or automatically repeats a write', async () => {
  const c = setup();
  c.req.method = 'PUT';
  c.req.params.operation = 'file';
  c.req.query.path = 'report.csv';
  c.fetch.mockResolvedValueOnce(new Response('sensitive error', { status: 409 }));
  c.fetch.mockResolvedValueOnce(new Response('different existing bytes'));
  await c.run();
  expect(c.fetch).toHaveBeenCalledTimes(2);
  expect(c.fetch.mock.calls[0][0].searchParams.has('overwrite')).toBe(false);
  expect(c.res.json).toHaveBeenCalledWith({ code: 'personal_files_409' });
});
it('preserves quota refusal and hides upstream diagnostics', async () => {
  const c = setup();
  c.fetch.mockResolvedValue(new Response('private', { status: 507 }));
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(507);
  expect(JSON.stringify(c.res.json.mock.calls)).not.toContain('private');
});
it('recovers an already saved identical file without another write', async () => {
  const c = setup();
  c.req.method = 'PUT';
  c.req.params.operation = 'file';
  c.req.query.path = 'report.csv';
  c.fetch.mockResolvedValueOnce(new Response('', { status: 409 }));
  c.fetch.mockResolvedValueOnce(new Response('synthetic'));
  await c.run();
  expect(c.res.json).toHaveBeenCalledWith({ saved: true, recovered: true });
  expect(c.fetch.mock.calls.map((call) => call[1].method)).toEqual(['PUT', 'GET']);
});
it('reports uncertain timeout without retrying', async () => {
  const c = setup();
  c.fetch.mockRejectedValue(new Error('private'));
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(503);
  expect(c.fetch).toHaveBeenCalledTimes(1);
});

it.each(['GET', 'PUT', 'POST'])(
  'protects snapshot storage from direct %s requests',
  async (method) => {
    const c = setup();
    c.req.method = method;
    c.req.query.path = '.starbridge-versions/private';
    await c.run();
    expect(c.res.status).toHaveBeenCalledWith(400);
    expect(c.fetch).not.toHaveBeenCalled();
  },
);
it('rejects invalid revision requests before reading user data', async () => {
  const c = setup();
  c.req.params.operation = 'revisions';
  c.req.method = 'POST';
  c.req.query = { path: 'report.xlsx', version: '../2', requestId: 'bad' };
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(400);
  expect(c.fetch).not.toHaveBeenCalled();
});
