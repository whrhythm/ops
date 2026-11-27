import type { Request, Response } from 'express';
import { createPersonalFilesHandler } from './personalFiles';
import { personalVersions } from './personalVersions';

jest.mock('./personalVersions', () => ({
  ...jest.requireActual('./personalVersions'),
  personalVersions: jest.fn(),
}));

const taskId = '11111111-1111-4111-8111-111111111111';
function setup(version?: string, duplicate = false) {
  const saved = { version: 2, ...(duplicate ? { source: { taskId, fileId: 'output' } } : {}) };
  const save = jest.fn().mockResolvedValue({ version: 3 });
  (personalVersions as jest.Mock).mockReturnValue({ list: async () => [saved], save });
  const fetch = jest.fn().mockResolvedValue(new globalThis.Response('document'));
  const req = {
    user: { provider: 'openid', openidId: 'owner', openidIssuer: 'https://login.test' },
    config: { endpoints: { agents: { durableOfficeTasks: true } } },
    method: 'POST', params: { operation: 'task-revision' },
    query: { path: 'report.docx', taskId, fileId: 'output', ...(version ? { version } : {}) },
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn() };
  res.status.mockReturnValue(res);
  const handler = createPersonalFilesHandler({
    url: 'https://platform.test/internal/v1/workspace', issuer: 'https://login.test',
    serviceToken: 'test-token', fetch,
    officeTasks: { url: 'https://code.test/account-usage', fetch, headers: async () => ({ Authorization: 'Bearer test' }) },
  });
  return { save, fetch, res, run: () => handler(req, res as unknown as Response, jest.fn()) };
}

it('refuses a stale save before reading the output or writing a version', async () => {
  const c = setup('2');
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(409);
  expect(c.fetch).not.toHaveBeenCalled();
  expect(c.save).not.toHaveBeenCalled();
});
it('recovers the same task save even when the expected head is stale', async () => {
  const c = setup('2', true);
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(200);
  expect(c.res.json).toHaveBeenCalledWith(expect.objectContaining({ version: 2 }));
  expect(c.fetch).not.toHaveBeenCalled();
  expect(c.save).not.toHaveBeenCalled();
});
it.each(['3', undefined])('saves the next version with compatible caller version %s', async version => {
  const c = setup(version);
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(201);
  expect(c.save).toHaveBeenCalledWith(3, expect.any(String), Buffer.from('document'), { taskId, fileId: 'output' });
});
