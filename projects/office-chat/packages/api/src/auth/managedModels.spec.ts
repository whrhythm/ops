import type { Request, Response } from 'express';
import { createManagedCredentialGuard, createManagedModelProvisioning } from './managedModels';

const identity = {
  _id: 'existing-user',
  provider: 'openid',
  openidId: 'subject',
  openidIssuer: 'https://identity.example/issuer/',
  tokenset: { access_token: 'private-access' },
};
function context(overrides = {}) {
  const req = {
    user: identity,
    method: 'GET',
    params: {},
    query: {},
    ...overrides,
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn() };
  res.status.mockReturnValue(res);
  return { req, res: res as unknown as Response, next: jest.fn() };
}
function setup(
  result = { subject: 'subject', issuer: 'https://identity.example/issuer', key: 'sk-secret' },
) {
  const fetcher = jest.fn().mockResolvedValue({ ok: true, json: async () => result });
  const updateUserKey = jest.fn().mockResolvedValue(undefined);
  const deps = {
    url: 'https://platform.example/internal/key',
    serviceToken: 'private-service',
    fetch: fetcher,
    updateUserKey,
  };
  return { deps, fetcher, updateUserKey, middleware: createManagedModelProvisioning(deps) };
}
describe('managed model login', () => {
  it('reuses the authenticated identity and only persists the encrypted-key input server side', async () => {
    const { middleware, fetcher, updateUserKey } = setup();
    const c = context();
    await middleware(c.req, c.res, c.next);
    expect(updateUserKey).toHaveBeenCalledWith({
      userId: 'existing-user',
      name: 'LiteLLM',
      value: '{"apiKey":"sk-secret"}',
    });
    expect(fetcher).toHaveBeenCalledWith(
      new URL('https://platform.example/internal/key'),
      expect.objectContaining({ method: 'POST', redirect: 'error' }),
    );
    expect(c.next).toHaveBeenCalledTimes(1);
    expect(c.res.json).not.toHaveBeenCalled();
  });
  it.each([
    { subject: 'other', issuer: identity.openidIssuer, key: 'sk-secret' },
    { subject: 'subject', issuer: 'https://other.example', key: 'sk-secret' },
    { subject: 'subject', issuer: identity.openidIssuer, key: '' },
  ])('rejects a mismatched provisioned identity or invalid key', async (result) => {
    const s = setup(result);
    const c = context();
    await s.middleware(c.req, c.res, c.next);
    expect(s.updateUserKey).not.toHaveBeenCalled();
    expect(c.next).not.toHaveBeenCalled();
    expect(c.res.status).toHaveBeenCalledWith(503);
  });
  it('fails closed and redacts network errors', async () => {
    const s = setup();
    s.fetcher.mockRejectedValue(new Error('sk-secret private-access'));
    const c = context();
    await s.middleware(c.req, c.res, c.next);
    expect(c.res.json).toHaveBeenCalledWith({ code: 'managed_model_setup_unavailable' });
    expect(s.updateUserKey).not.toHaveBeenCalled();
  });
  it('does not forward credentials over HTTP or without authenticated OIDC identity', async () => {
    const s = setup();
    const c = context();
    await createManagedModelProvisioning({ ...s.deps, url: 'http://platform.example' })(
      c.req,
      c.res,
      c.next,
    );
    await s.middleware(context({ user: undefined }).req, c.res, c.next);
    expect(s.fetcher).not.toHaveBeenCalled();
    expect(s.updateUserKey).not.toHaveBeenCalled();
  });
  it('preserves upstream behavior when management is unconfigured', async () => {
    const s = setup();
    const c = context();
    await createManagedModelProvisioning({ ...s.deps, url: undefined })(c.req, c.res, c.next);
    expect(s.fetcher).not.toHaveBeenCalled();
    expect(c.next).toHaveBeenCalledTimes(1);
  });
});
describe('managed credential mutation guard', () => {
  it.each([
    { method: 'PUT', body: { name: 'LiteLLM', value: 'arbitrary' } },
    { method: 'DELETE', params: { name: 'LiteLLM' } },
    { method: 'DELETE', query: { all: 'true' } },
  ])('prevents replacing or deleting a platform-owned key', (input) => {
    const c = context(input);
    createManagedCredentialGuard({ enabled: true })(c.req, c.res, c.next);
    expect(c.res.status).toHaveBeenCalledWith(403);
    expect(c.next).not.toHaveBeenCalled();
  });
  it('allows ordinary user-managed keys and read requests', () => {
    for (const input of [{ method: 'PUT', body: { name: 'other' } }, { method: 'GET' }]) {
      const c = context(input);
      createManagedCredentialGuard({ enabled: true })(c.req, c.res, c.next);
      expect(c.next).toHaveBeenCalledTimes(1);
    }
  });
});
