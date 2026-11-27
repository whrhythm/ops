import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import Modal from './Modal';

type Application = {
  id: string;
  name: string;
  applicant_subject: string;
  revision: number;
  status: 'pending' | 'issued' | 'revoked' | 'activated';
  code_expires_at: string | null;
};

const statusLabels = { pending: '待审核', issued: '待激活', revoked: '已撤销', activated: '已激活' };
const path = '/v1/admin/enterprises/applications';

export default function Enterprises() {
  const [rows, setRows] = useState<Application[]>();
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [draft, setDraft] = useState<Application>();
  const [issued, setIssued] = useState<{ code: string; expires_at: string }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    setRows(undefined);
    void api<Application[]>(`${path}?page=${page}`).then((result) => {
      if (active) setRows(result);
    }).catch(() => {
      if (active) setError('暂时无法读取企业申请。');
    });
    return () => { active = false; };
  }, [page, revision]);

  async function issue(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft || busy) return;
    const form = new FormData(event.currentTarget);
    const owner = String(form.get('owner') ?? '').trim();
    setBusy(true);
    setError('');
    try {
      setIssued(await api<NonNullable<typeof issued>>(`${path}/${draft.id}/issue`, 'POST', {
        revision: draft.revision,
        owner_account_id: owner ? Number(owner) : null,
        code_days: Number(form.get('code_days')),
        trial_days: Number(form.get('trial_days')),
        limits: {
          seats: Number(form.get('seats')),
          storage_gib: Number(form.get('storage_gib')),
          compute_minutes: Number(form.get('compute_minutes')),
        },
      }));
      setRevision((value) => value + 1);
    } catch {
      setError('签发未确认。请关闭窗口并刷新，若已经签发但未收到授权码，可撤销后重新签发。');
    } finally {
      setBusy(false);
    }
  }

  async function revoke(row: Application) {
    if (busy || !window.confirm(`撤销「${row.name}」尚未使用的授权码？`)) return;
    setBusy(true);
    setError('');
    try {
      await api(`${path}/${row.id}/revoke`, 'POST', { revision: row.revision });
    } catch {
      setError('撤销未确认，请刷新查看当前状态。');
    } finally {
      setBusy(false);
      setRevision((value) => value + 1);
    }
  }

  return (
    <section className="panel" aria-label="企业申请">
      <div className="account-actions">
        <h2>企业申请</h2>
        <button className="subtle" disabled={busy} onClick={() => {
          setError('');
          setRevision((value) => value + 1);
        }}>刷新</button>
      </div>
      {error && !draft && <p className="error" role="alert">{error}</p>}
      {!rows ? (error ? null : <p role="status">正在读取申请…</p>) : rows.length === 0 ? (
        <p>这一页没有申请。</p>
      ) : (
        <div className="inventory-scroll">
          <table className="inventory-table">
            <thead><tr><th>企业</th><th>申请人账号标识</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.name}</td>
                  <td><span className="inventory-meta">{row.applicant_subject}</span></td>
                  <td>{statusLabels[row.status]}{row.code_expires_at && row.status === 'issued' &&
                    <span className="inventory-meta">有效至 {new Date(row.code_expires_at).toLocaleString()}</span>}
                  </td>
                  <td>
                    {(row.status === 'pending' || row.status === 'revoked') &&
                      <button className="subtle" disabled={busy} onClick={() => { setError(''); setDraft(row); }}>
                        审核并签发
                      </button>}
                    {row.status === 'issued' &&
                      <button className="subtle" disabled={busy} onClick={() => void revoke(row)}>撤销授权码</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="inventory-pagination">
        <button disabled={page === 1 || busy} onClick={() => setPage((value) => value - 1)}>上一页</button>
        <span>第 {page} 页</span>
        <button disabled={rows?.length !== 25 || busy} onClick={() => setPage((value) => value + 1)}>下一页</button>
      </div>
      {draft && (
        <Modal title={issued ? '授权码已签发' : `审核 ${draft.name}`} onClose={() => {
          if (busy) return;
          setDraft(undefined);
          setIssued(undefined);
          setError('');
          setRevision((value) => value + 1);
        }}>
          {issued ? (
            <>
              <label>授权码<input readOnly value={issued.code} onFocus={(event) => event.currentTarget.select()} /></label>
              <p>有效至 {new Date(issued.expires_at).toLocaleString()}。请复制并通过受控渠道交给指定账号。</p>
            </>
          ) : (
            <form className="account-form" onSubmit={(event) => void issue(event)}>
              <label>代开所有者的 Authentik 用户编号<input name="owner" type="number" min={1} placeholder="留空则由申请人激活" /></label>
              <label>授权码有效天数<input name="code_days" required type="number" min={1} max={30} /></label>
              <label>试用天数<input name="trial_days" required type="number" min={1} max={90} /></label>
              <label>席位<input name="seats" required type="number" min={1} max={10000} /></label>
              <label>存储上限（GiB）<input name="storage_gib" required type="number" min={0} max={1000000} /></label>
              <label>计算上限（分钟）<input name="compute_minutes" required type="number" min={0} max={1000000} /></label>
              {error && <p className="error account-help" role="alert">{error}</p>}
              <footer><button type="submit" disabled={busy}>确认签发</button></footer>
            </form>
          )}
        </Modal>
      )}
    </section>
  );
}
