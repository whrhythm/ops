import type { Request, Response } from 'express';
import { createManagedUsageHandler, managedUsageEnabled } from './managedUsage';

function setup() {
  const req = {
    user: { openidId: 'owner' },
    query: { subject: 'victim' },
    headers: { Authorization: 'attacker' },
  } as unknown as Request;
  const res = { setHeader: jest.fn(), status: jest.fn(), json: jest.fn() };
  res.status.mockReturnValue(res);
  const source = {
    plan_name: 'Basic',
    wallet: {
      status: 'stale',
      remaining_usd_micros: 1234567,
      spent_usd_micros: 10,
      total_tokens: 7,
      secret: 'private',
    },
    code_execution: { used_runs: 2, code_runs_monthly: 30 },
  };
  const fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => source });
  const headers = jest.fn().mockResolvedValue({ Authorization: 'signed-owner' });
  const handler = createManagedUsageHandler({
    url: 'https://platform.test/account-usage',
    fetch,
    headers,
  });
  return {
    req,
    res,
    source,
    fetch,
    headers,
    run: () => handler(req, res as unknown as Response, jest.fn()),
  };
}

it('binds to authenticated identity, ignores caller selectors and only returns whitelisted amounts', async () => {
  const c = setup();
  await c.run();
  expect(c.headers).toHaveBeenCalledWith(c.req);
  expect(c.fetch).toHaveBeenCalledWith(
    new URL('https://platform.test/account-usage'),
    expect.objectContaining({
      method: 'GET',
      headers: { Authorization: 'signed-owner' },
      redirect: 'error',
    }),
  );
  expect(c.res.json).toHaveBeenCalledWith(
    expect.objectContaining({
      status: 'stale',
      remainingUsd: 1.234567,
      totalTokens: 7,
      code: expect.objectContaining({ used: 2, limit: 30 }),
    }),
  );
  expect(JSON.stringify(c.res.json.mock.calls)).not.toContain('private');
  expect(c.res.setHeader).toHaveBeenCalledWith('Cache-Control', 'no-store');
});

it('does not turn outages into a zero balance or expose provider errors', async () => {
  const c = setup();
  c.fetch.mockRejectedValue(new Error('private diagnostic'));
  await c.run();
  expect(c.res.status).toHaveBeenCalledWith(503);
  expect(c.res.json).toHaveBeenCalledWith({ code: 'managed_usage_unavailable' });
  expect(c.fetch).toHaveBeenCalledTimes(1);
});

it('fails closed before a request if no signed identity is available', async () => {
  const c = setup();
  c.headers.mockResolvedValue({});
  await c.run();
  expect(c.fetch).not.toHaveBeenCalled();
  expect(c.res.status).toHaveBeenCalledWith(503);
});

it('requires a deployment-owned HTTPS URL without embedded credentials', () => {
  for (const url of [undefined, 'bad', 'http://example.test', 'https://user:secret@example.test']) {
    expect(managedUsageEnabled(url)).toBe(false);
  }
  expect(managedUsageEnabled('https://platform.test/usage')).toBe(true);
});

it('keeps the daily trial separate from the model wallet and omits unrelated fields', async () => {
  const c = setup();
  Object.assign(c.source, {daily_free:{limit:100, used:30, reserved:20, remaining:50,
    timezone:'Asia/Shanghai', models:['demo'], internal:'secret'}});
  await c.run();
  expect(c.res.json).toHaveBeenCalledWith(expect.objectContaining({ remainingUsd:1.234567,
    dailyFree:{limit:100, used:30, reserved:20, remaining:50, models:['demo']}}));
});

it('forwards only explicit dated consent and uses the authenticated account', async () => {
  const c = setup();
  c.req.method = 'POST';
  c.req.body = {day:'2026-11-25', confirm:false};
  await c.run();
  expect(c.fetch).not.toHaveBeenCalled();
  c.req.body = {day:'2026-11-25', confirm:true, subject:'someone-else'};
  c.fetch.mockResolvedValue({ok:true,json:async()=>({authorized:true,day:'2026-11-25'})});
  await c.run();
  expect(c.fetch).toHaveBeenCalledWith(new URL('https://platform.test/account-usage/consent'),
    expect.objectContaining({headers:{Authorization:'signed-owner','Content-Type':'application/json'},
      body:JSON.stringify({day:'2026-11-25',confirm:true})}));
  expect(c.res.json).toHaveBeenCalledWith({authorized:true,day:'2026-11-25'});
});
