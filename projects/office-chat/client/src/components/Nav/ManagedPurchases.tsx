import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { apiBaseUrl, QueryKeys } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';

type Plan = {id: string; version: string; name: string; amount_minor: number; currency: string; code_runs_monthly?: number; model_credit_usd_micros: number};
type Order = {id: string; plan: Plan; status: string; payment_status: string; error_code?: string | null};
type Account = {enabled: boolean; payment_provider?: string; plans?: Plan[]; orders?: Order[]; active_order?: Order};

export default function ManagedPurchases({ open, onReturn }: { open: boolean; onReturn?: () => void }) {
  const { token, user } = useAuthContext();
  const t = useLocalize();
  const client = useQueryClient();
  const [selected, setSelected] = useState<Plan>();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [orderId, setOrderId] = useState<string>();
  const attempt = useRef<string>();
  async function request(suffix = '', body?: object, key?: string) {
    const response = await fetch(apiBaseUrl() + '/api/balance/purchases' + suffix, {
      method: body ? 'POST' : 'GET', redirect: 'error',
      headers: { Authorization: 'Bearer ' + token,
        ...(body ? {'Content-Type': 'application/json'} : {}), ...(key ? {'Idempotency-Key': key} : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw Error('purchase_failed');
    return response.json();
  }
  const account = useQuery<Account>(['managed-purchases', user?.id], () => request(), {
    enabled: open && !!token, retry: false, staleTime: 0,
    refetchInterval: data => {
      const id = orderId ?? data?.active_order?.id;
      const state = data?.orders?.find(o => o.id === id)?.status;
      return open && id && !['ready', 'cancelled', 'failed'].includes(state ?? '') ? 3000 : false;
    },
    onSuccess: data => {
      if (data.orders?.some(o => o.id === orderId && o.status === 'ready')) {
        void client.invalidateQueries([QueryKeys.managedUsage, user?.id]);
      }
    },
  });
  const current = account.data?.orders?.find(o => o.id === orderId) ?? account.data?.active_order ?? account.data?.orders?.[0];
  async function pay() {
    if (!selected || !user || busy) return;
    setBusy(true); setFailed(false);
    const storage = 'starbridge-payment-attempt-' + user.id + '-' + selected.id + '-' + selected.version;
    try {
      attempt.current ??= sessionStorage.getItem(storage) ?? crypto.randomUUID();
      sessionStorage.setItem(storage, attempt.current);
      const order: Order = await request('/create', {plan_id: selected.id, plan_version: selected.version}, attempt.current);
      setOrderId(order.id);
      await request('/' + order.id + '/checkout', {});
      setSelected(undefined);
      await account.refetch();
    } catch { setFailed(true); }
    finally { setBusy(false); }
  }
  async function replay() {
    if (!current || busy) return;
    setBusy(true); setFailed(false);
    try {
      await request('/' + current.id + '/checkout', {});
      await account.refetch();
    } catch { setFailed(true); }
    finally { setBusy(false); }
  }
  async function retry() {
    if (!current || busy) return;
    setBusy(true); setFailed(false);
    try {
      setOrderId(current.id);
      await request('/' + current.id + '/retry', {});
      await account.refetch();
    } catch { setFailed(true); }
    finally { setBusy(false); }
  }
  if (account.isLoading) return <p role="status">{t('com_purchase_loading')}</p>;
  if (!account.data?.enabled || account.data.payment_provider !== 'xunhupay_local') return <p role="status">{t('com_purchase_unavailable')}</p>;
  const price = (p: Plan) => (p.currency === 'CNY' ? '¥' : '') + (p.amount_minor / 100).toFixed(2) + ' ' + p.currency;
  const ready = current?.status === 'ready';
  return <section className="space-y-3 border-t border-border-light pt-4" aria-label={t('com_purchase_heading')}>
    <p className="text-xs text-text-secondary">{t('com_purchase_simulation')}</p>
    {failed && <p role="alert">{t('com_purchase_failed')}</p>}
    {current && <div className="space-y-2 rounded-lg border border-border-light p-3" aria-live="polite">
      <p className="font-medium">{current.plan.name}</p>
      <p>{t(ready ? 'com_purchase_ready' : current.status === 'cancelled' ? 'com_purchase_cancelled' : current.status === 'failed' ? 'com_purchase_activation_failed' :
        current.payment_status === 'confirmed' ? 'com_purchase_applying' : 'com_purchase_pending')}</p>
      {ready && <button type="button" className="btn btn-neutral" onClick={onReturn}>{t('com_purchase_return')}</button>}
      {current.status === 'pending_payment' && current.payment_status !== 'confirmed' && <button type="button" className="btn btn-neutral" disabled={busy} onClick={() => void replay()}>{t('com_purchase_continue_payment')}</button>}
      {current.status === 'failed' && current.payment_status === 'confirmed' &&
        !current.error_code?.includes('requires_review') &&
        <button type="button" className="btn btn-neutral" disabled={busy} onClick={() => void retry()}>{t('com_purchase_retry')}</button>}
    </div>}
    {selected ? <div className="space-y-2 rounded-lg border border-border-light p-3">
      <p>{selected.name} · {price(selected)}</p>
      <p>{t('com_purchase_credit', {0: (selected.model_credit_usd_micros / 1000000).toFixed(6)})}</p>
      {selected.code_runs_monthly != null && <p>{t('com_purchase_runs',{0:selected.code_runs_monthly})}</p>}
      <div className="flex flex-wrap gap-2">
        <button className="btn btn-primary" disabled={busy} type="button" onClick={() => void pay()}>{t('com_purchase_confirm')}</button>
        <button className="btn btn-neutral" disabled={busy} type="button" onClick={() => setSelected(undefined)}>{t('com_ui_cancel')}</button>
      </div>
    </div> : (!current || ready || current.status === 'cancelled') && account.data.plans?.filter(p => p.model_credit_usd_micros > 0).map(p =>
      <button key={p.id} type="button" className="btn btn-neutral w-full justify-between" onClick={() => {
        attempt.current = undefined;
        if (user) sessionStorage.removeItem('starbridge-payment-attempt-' + user.id + '-' + p.id + '-' + p.version);
        setSelected(p);
      }}>
        <span>{p.name}</span><span>{price(p)}</span>
      </button>)}
  </section>;
}
