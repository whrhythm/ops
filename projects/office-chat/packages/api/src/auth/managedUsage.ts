import type { RequestHandler } from 'express';

export interface ManagedUsageDependencies {
  url?: string;
  fetch: (input: URL, init: RequestInit) => Promise<Response>;
  headers: (req: Parameters<RequestHandler>[0]) => Promise<Record<string, string>>;
}

export function managedUsageEnabled(url?: string): boolean {
  if (!url) return false;
  try {
    const target = new URL(url);
    return target.protocol === 'https:' && !target.username && !target.password;
  } catch {
    return false;
  }
}

/** Only the authenticated request supplies identity; never forward browser headers or query. */
export function createManagedUsageHandler(deps: ManagedUsageDependencies): RequestHandler {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    if (!managedUsageEnabled(deps.url)) {
      res.status(404).json({ code: 'managed_usage_disabled' });
      return;
    }
    try {
      const headers = await deps.headers(req);
      if (!headers.Authorization) throw new Error('identity_unavailable');
      if (req.method === 'POST' && req.path === '/managed/refill') {
        const {confirm, month, tokens} = req.body ?? {};
        if (confirm !== true || typeof month !== 'string' || !/^\d{4}-\d{2}$/.test(month) ||
            !Number.isSafeInteger(tokens) || tokens <= 0) {
          res.status(400).json({code:'invalid_refill'}); return;
        }
        const target = new URL(deps.url!);
        target.pathname = target.pathname.replace(/\/$/, '')+'/refill';
        const r = await deps.fetch(target,{method:'POST',headers:{...headers,'Content-Type':'application/json'},
          body:JSON.stringify({confirm,month,tokens}),redirect:'error',signal:AbortSignal.timeout(8000)});
        if(!r.ok){res.status([403,409].includes(r.status)?r.status:503).json({code:'refill_unavailable'});return;}
        const receipt = await r.json();
        if(receipt.tokens!==tokens || receipt.month!==month || typeof receipt.receipt!=='string') throw Error();
        res.json({tokens,month,receipt:receipt.receipt});return;
      }
      if (req.method === 'POST') {
        const {day, confirm} = req.body ?? {};
        if (confirm !== true || typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day)) {
          res.status(400).json({code:'invalid_confirmation'}); return;
        }
        const url=new URL(deps.url!);
        url.pathname=url.pathname.replace(/\/$/, '')+'/consent';
        const receipt=await deps.fetch(url,{method:'POST',headers:{...headers,'Content-Type':'application/json'},
          body:JSON.stringify({day,confirm:true}),redirect:'error',signal:AbortSignal.timeout(8000)});
        if (!receipt.ok) {res.status(receipt.status===409?409:503).json({code:'consent_unavailable'});return;}
        const accepted=await receipt.json();
        if (accepted.authorized!==true || accepted.day!==day) throw Error('invalid_receipt');
        res.json({authorized:true,day});return;
      }
      const result = await deps.fetch(new URL(deps.url!), {
        method: 'GET',
        headers,
        redirect: 'error',
        signal: AbortSignal.timeout(8000),
      });
      if (!result.ok) throw new Error('usage_unavailable');
      const source = await result.json();
      const wallet = source.wallet;
      const code = source.code_execution;
      if (!wallet || !['ok', 'stale', 'unknown'].includes(wallet.status) || !code) {
        throw new Error('invalid_usage');
      }
      const number = (value: unknown): number | null =>
        typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
      const free = source.daily_free;
      if (free != null && (!['limit', 'remaining', 'used', 'reserved'].every(
        key => Number.isSafeInteger(free[key]) && free[key] >= 0,
      ) || free.timezone !== 'Asia/Shanghai' || !Array.isArray(free.models)
        || !free.models.every((m: unknown) => typeof m === 'string' && m.length <= 200))) {
        throw new Error('invalid_daily_free');
      }
      if (free?.refill) {
        const f=free.refill;
        if (!['remaining','used','reserved','pack'].every(k=>Number.isSafeInteger(f[k]) && f[k]>=0) ||
          typeof f.month!=='string' || !/^\d{4}-\d{2}$/.test(f.month) || typeof f.can_refill!=='boolean') throw Error();
        res.json({status:'ok',planName:'',observedAt:null,remainingUsd:null,spentUsd:null,totalTokens:null,
          code:{used:null,limit:null,seconds:null,concurrent:null,period:''},dailyFree:{limit:free.limit,remaining:free.remaining,used:free.used,reserved:free.reserved,
          day:free.day,models:free.models,poolPaused:free.pool_paused,refill:{remaining:f.remaining,used:f.used,
          reserved:f.reserved,pack:f.pack,month:f.month,can_refill:f.can_refill}}});return;
      }
      res.json({
        ...(free == null ? {} : { dailyFree: {
          limit: free.limit, remaining: free.remaining, used: free.used, reserved: free.reserved,
          models: free.models, ...(typeof free.day === 'string' ? {day: free.day} : {}),
          ...(typeof free.balance_authorized === 'boolean' ? {balanceAuthorized: free.balance_authorized} : {}),
          ...(typeof free.pool_paused === 'boolean' ? {poolPaused: free.pool_paused} : {}),
        } }),
        planName: typeof source.plan_name === 'string' ? source.plan_name.slice(0, 100) : '',
        status: wallet.status,
        observedAt: typeof wallet.observed_at === 'string' ? wallet.observed_at : null,
        remainingUsd:
          number(wallet.remaining_usd_micros) == null
            ? null
            : wallet.remaining_usd_micros / 1000000,
        spentUsd:
          number(wallet.spent_usd_micros) == null ? null : wallet.spent_usd_micros / 1000000,
        totalTokens: number(wallet.total_tokens),
        code: {
          used: number(code.used_runs),
          limit: number(code.code_runs_monthly),
          seconds: number(code.code_timeout_seconds),
          concurrent: number(code.code_concurrency),
          period: typeof code.period === 'string' ? code.period : '',
        },
      });
    } catch {
      res.status(503).json({ code: 'managed_usage_unavailable' });
    }
  };
}
