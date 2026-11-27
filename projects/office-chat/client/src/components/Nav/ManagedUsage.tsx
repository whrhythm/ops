import { useQuery } from '@tanstack/react-query';
import { apiBaseUrl, dataService, QueryKeys } from 'librechat-data-provider';
import { OGDialog, OGDialogContent, OGDialogHeader, OGDialogTitle } from '@librechat/client';
import { useEffect, useState, type RefObject } from 'react';
import { useAuthContext, useLocalize } from '~/hooks';

export default function ManagedUsage({ open, onOpenChange, triggerRef }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const t = useLocalize();
  const { user, token } = useAuthContext();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<'done' | 'error' | null>(null);
  useEffect(() => {
    if (!open) { setConfirming(false); setNotice(null); }
  }, [open]);
  useEffect(() => {
    const follow = (event: MouseEvent) => {
      const link = event.target instanceof Element ? event.target.closest('a') : null;
      if (!/^#starbridge-billing(?:-(?:personal|shared|model|plans))?$/.test(link?.getAttribute('href') ?? '')) return;
      event.preventDefault();
      onOpenChange(true);
    };
    document.addEventListener('click', follow);
    return () => document.removeEventListener('click', follow);
  }, [onOpenChange]);
  const query = useQuery([QueryKeys.managedUsage, user?.id], dataService.getManagedUsage,
    { enabled:open && !!user?.id, refetchInterval:open ? 30000 : false, staleTime:0, retry:false });
  const free = query.data?.dailyFree;
  const refill = free?.refill;
  async function submit() {
    if (!refill?.can_refill || busy) return;
    setBusy(true); setNotice(null);
    try {
      const r = await fetch(apiBaseUrl()+'/api/balance/managed/refill', {
        method:'POST', redirect:'error',
        headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
        body:JSON.stringify({month:refill.month,tokens:refill.pack,confirm:true}),
      });
      if (!r.ok) throw Error();
      const receipt = await r.json();
      if (receipt.tokens !== refill.pack || receipt.month !== refill.month || !receipt.receipt) throw Error();
      setNotice('done'); setConfirming(false);
    } catch { setNotice('error'); }
    finally { await query.refetch(); setBusy(false); }
  }
  return <OGDialog open={open} onOpenChange={onOpenChange} triggerRef={triggerRef}>
    <OGDialogContent className="w-[calc(100%-2rem)] max-w-md" style={{maxHeight:'90dvh',overflowY:'auto'}}>
      <OGDialogHeader><OGDialogTitle>{t('com_starbridge_token_usage')}</OGDialogTitle></OGDialogHeader>
      {(!free || !refill || query.isError) && <p role="status">{t(query.isLoading?'com_starbridge_loading':'com_starbridge_tokens_unavailable')}</p>}
      {free && refill && <div className="space-y-4 text-sm">
        <section className="space-y-2 rounded-xl border border-border-light p-4">
          <h3>{t('com_starbridge_free_tokens')}</h3>
          <p className="text-3xl font-semibold tabular-nums">{free.remaining.toLocaleString()} <small>tokens</small></p>
          <progress className="w-full" aria-label={t('com_starbridge_free_tokens')} max={free.limit || 1} value={free.remaining}/>
          <p className="text-text-secondary">{t('com_starbridge_daily_total',{0:free.limit.toLocaleString(),1:free.models.join(', ')})}</p>
          {free.poolPaused && <p role="status">{t('com_starbridge_token_pause')}</p>}
        </section>
        <section className="space-y-2 rounded-xl border border-border-light p-4">
          <h3>{t('com_starbridge_refilled')}</h3>
          <p className="text-xl font-semibold tabular-nums">{refill.remaining.toLocaleString()} tokens</p>
          <p className="text-text-secondary">{t('com_starbridge_token_order')}</p>
          {refill.can_refill && !confirming && <button type="button" className="btn btn-primary" onClick={()=>{setConfirming(true);setNotice(null);}}>{t('com_starbridge_refill')}</button>}
          {confirming && refill.can_refill && <div className="space-y-2">
            <p>{t('com_starbridge_refill_offer',{0:refill.pack.toLocaleString()})}</p>
            <div className="flex gap-2"><button type="button" disabled={busy || query.isError} className="btn btn-primary" onClick={()=>void submit()}>{t('com_starbridge_confirm_refill')}</button>
            <button type="button" disabled={busy} className="btn btn-neutral" onClick={()=>setConfirming(false)}>{t('com_ui_cancel')}</button></div>
          </div>}
          {!refill.can_refill && <p>{t('com_starbridge_no_refill')}</p>}
          {notice && <p role="status">{t(notice==='done'?'com_starbridge_refill_done':'com_starbridge_refill_error')}</p>}
        </section>
        <details><summary>{t('com_starbridge_details')}</summary>
          <p>{t('com_starbridge_free_used')} · {free.used.toLocaleString()} tokens</p>
          <p>{t('com_starbridge_refill_used')} · {refill.used.toLocaleString()} tokens</p>
          <p>{t('com_starbridge_held')} · {(free.reserved+refill.reserved).toLocaleString()} tokens</p>
        </details>
      </div>}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn btn-neutral" disabled={query.isFetching || busy} onClick={()=>void query.refetch()}>{t('com_starbridge_refresh')}</button>
        <button type="button" className="btn btn-neutral" onClick={()=>onOpenChange(false)}>{t('com_starbridge_resume')}</button>
      </div>
    </OGDialogContent>
  </OGDialog>;
}
