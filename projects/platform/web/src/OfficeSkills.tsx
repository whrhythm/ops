import { useEffect, useState } from 'react';
import { api } from './api';

type Package = {
  name: string; description: string; version: string;
  source: { repository: string; commit: string; directory: string };
  files: Record<string, string>; permissions: string[]; environment_ids: string[];
};
type Review = {
  id: string; revision: number; digest: string;
  state: 'pending' | 'approved' | 'installed' | 'rejected' | 'revoked';
  package: Package;
  decisions: Array<{ action: string; by: string; at: string; digest: string }>;
};
type Account = { environment_id: string; subject: string; workspace_name: string };
const endpoint = '/v1/admin/office-skills';
const labels = { pending: '待审核', approved: '已批准，待安装', installed: '已安装', rejected: '已驳回', revoked: '已停用' };
const permissions: Record<string, string> = { 'selected-files.read': '读取用户选中的文件', 'python.execute': '使用现有 Python 计算' };

export default function OfficeSkills() {
  const [rows, setRows] = useState<Review[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selected, setSelected] = useState('');
  const [draft, setDraft] = useState<Package>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [ack, setAck] = useState<string>();

  async function load() {
    const [reviews, users] = await Promise.all([
      api<{ reviews: Review[] }>(endpoint), api<{ items: Account[] }>('/v1/admin/users?limit=100'),
    ]);
    setRows(reviews.reviews);
    setAccounts(users.items);
  }
  async function perform(action: () => Promise<void>) {
    setError(''); setBusy(true);
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : '操作未完成。'); }
    finally { setBusy(false); }
  }
  useEffect(() => { void perform(load); }, []);

  async function decide(row: Review, action: string) {
    await api(endpoint + '/' + row.id + '/decision', 'POST', {
      revision: row.revision, digest: row.digest, action, acknowledged: true,
    });
    setAck(undefined);
    await load();
  }

  return <section className="panel" aria-label="办公 Skill 管理">
    <div className="panel-heading"><h2>办公 Skill</h2>
      <button className="subtle" disabled={busy} onClick={() => void perform(load)}>刷新</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    <details><summary>提交固定版本</summary>
      <label>审查包 JSON<input type="file" accept=".json,application/json" disabled={busy}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          void perform(async () => {
            if (file.size > 512 * 1024) throw new Error('审查包过大。');
            setDraft(JSON.parse(await file.text()) as Package);
          });
        }} /></label>
      {draft && <p>{draft.name} · {draft.version} · {draft.source?.repository}</p>}
      <label>试用账号<select value={selected} onChange={(e) => setSelected(e.target.value)} disabled={busy}>
        <option value="">请选择专用测试账号</option>
        {accounts.map((user) => <option key={user.environment_id} value={user.environment_id}>
          {user.workspace_name} · {user.subject}</option>)}
      </select></label>
      <button disabled={busy || !draft || !selected} onClick={() => void perform(async () => {
        await api(endpoint, 'POST', { ...draft, environment_ids: [selected] });
        setDraft(undefined); await load();
      })}>提交待审</button>
    </details>
    {!busy && rows.length === 0 && <p>暂无 Skill 申请。</p>}
    {rows.map((row) => <article key={row.id} aria-label={row.package.name}>
      <h3>{row.package.name} · {row.package.version} · {labels[row.state]}</h3>
      <p>{row.package.description}</p>
      <dl><dt>来源</dt><dd>{row.package.source.repository} / {row.package.source.directory}</dd>
        <dt>固定提交</dt><dd><code>{row.package.source.commit}</code></dd>
        <dt>内容与授权摘要</dt><dd><code>{row.digest}</code></dd>
        <dt>申请权限</dt><dd>{row.package.permissions.map((item) => permissions[item] ?? item).join('、')}</dd>
        <dt>试用范围</dt><dd>{row.package.environment_ids.map((id) => {
          const account = accounts.find((user) => user.environment_id === id);
          return account ? account.workspace_name + ' · ' + account.subject : id;
        }).join('、')}</dd></dl>
      <details><summary>查看指引、依赖和许可证（{Object.keys(row.package.files).length} 份）</summary>
        {Object.entries(row.package.files).map(([name, content]) => <details key={name}>
          <summary>{name}</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{content}</pre>
        </details>)}</details>
      <details><summary>审核记录</summary>{row.decisions.map((decision, index) =>
        <p key={index}>{decision.action} · {decision.by} · {new Date(decision.at).toLocaleString()}</p>)}</details>
      {['pending', 'approved', 'installed'].includes(row.state) && <>
        <label><input type="checkbox" checked={ack === row.id} onChange={(e) => setAck(e.target.checked ? row.id : undefined)} />
          已核对这个版本的完整内容、工具权限和试用账号</label>
        {row.state === 'pending' && <>
          <button disabled={busy || ack !== row.id} onClick={() => void perform(() => decide(row, 'approve'))}>批准准入</button>
          <button className="subtle" disabled={busy || ack !== row.id} onClick={() => void perform(() => decide(row, 'reject'))}>驳回</button>
        </>}
        {row.state === 'approved' && <button disabled={busy || ack !== row.id} onClick={() => void perform(() => decide(row, 'install'))}>安装给上述试用账号</button>}
        {['approved', 'installed'].includes(row.state) && <button className="subtle" disabled={busy || ack !== row.id}
          onClick={() => void perform(() => decide(row, 'revoke'))}>停用该版本</button>}
      </>}
    </article>)}
  </section>;
}
