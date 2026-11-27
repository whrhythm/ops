import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Input } from '@librechat/client';
import { apiBaseUrl } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';

type Invitation = {
  id: string;
  enterprise_name: string;
  username: string;
  role: string;
  status: string;
};
type Enterprise = { id: string; role: string; status: string };
type Asset = { id: string; name: string; head: number };
type Version = { number: number; size: number };
type Save = { file: File; requestId: string; asset?: Asset };

class RequestError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}

async function encodeFile(file: File) {
  if (file.size < 1 || file.size > 1024 * 1024)
    throw new RequestError(413, 'enterprise_file_size_limit');
  const bytes = new Uint8Array(await file.arrayBuffer());
  const chunks: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)));
  }
  return btoa(chunks.join(''));
}

export default function EnterpriseCollaboration({ enterprise }: { enterprise?: Enterprise }) {
  const t = useLocalize();
  const { token, user } = useAuthContext();
  const cache = useQueryClient();
  const prefix = ['enterprise-collaboration', user?.id];
  const root = enterprise?.id;
  const owner = enterprise?.role === 'owner';
  const canWrite = enterprise?.status === 'trial' && (owner || enterprise.role === 'editor');
  const [username, setUsername] = useState('');
  const [role, setRole] = useState('viewer');
  const [asset, setAsset] = useState<Asset>();
  const [pending, setPending] = useState<Save>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string>();

  async function request(path: string, body?: object) {
    const response = await fetch(`${apiBaseUrl()}/api/enterprise/${path}`, {
      method: body ? 'POST' : 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      const reason = await response.json().catch(() => ({}));
      throw new RequestError(response.status, reason.code ?? 'enterprise_unavailable');
    }
    return response;
  }

  const inbox = useQuery(
    [...prefix, 'invites'],
    async () => (await request('invitations')).json() as Promise<Invitation[]>,
    {
      enabled: Boolean(token && user?.id),
      retry: false,
      cacheTime: 0,
    },
  );
  const available = inbox.isSuccess;
  const members = useQuery(
    [...prefix, root, 'members'],
    async () => (await request(`${root}/members`)).json() as Promise<Invitation[]>,
    {
      enabled: available && Boolean(root && owner),
      retry: false,
      cacheTime: 0,
    },
  );
  const files = useQuery(
    [...prefix, root, 'files'],
    async () => (await request(`${root}/files`)).json() as Promise<Asset[]>,
    {
      enabled: available && Boolean(root),
      retry: false,
      cacheTime: 0,
    },
  );
  const versions = useQuery(
    [...prefix, root, asset?.id],
    async () => (await request(`${root}/files/${asset?.id}/versions`)).json() as Promise<Version[]>,
    {
      enabled: available && Boolean(root && asset),
      retry: false,
      cacheTime: 0,
    },
  );

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setFailure(undefined);
    try {
      await action();
    } catch (error) {
      const code = error instanceof RequestError ? error.code : '';
      setFailure(
        t(
          code === 'enterprise_file_version_conflict'
            ? 'com_e2_conflict'
            : code === 'enterprise_seats_full'
              ? 'com_e2_seats_full'
              : code === 'enterprise_invitee_unavailable'
                ? 'com_e2_unknown_account'
                : code === 'enterprise_file_size_limit'
                  ? 'com_e2_size'
                  : 'com_enterprise_failed',
        ),
      );
    } finally {
      setBusy(false);
    }
    await Promise.all([
      cache.invalidateQueries(prefix),
      cache.invalidateQueries(['enterprise-overview', user?.id]),
    ]);
  }

  function save(submission: Save) {
    setPending(submission);
    void run(async () => {
      const { file, requestId, asset: current } = submission;
      const payload = { content: await encodeFile(file), request_id: requestId };
      const response = current
        ? await request(`${root}/files/${current.id}/versions`, { ...payload, base: current.head })
        : await request(`${root}/files`, { ...payload, name: file.name });
      setPending(undefined);
      setAsset((await response.json()) as Asset);
    });
  }

  function download(selected: Asset, number: number) {
    void run(async () => {
      const response = await request(`${root}/files/${selected.id}/versions/${number}`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = selected.name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    });
  }

  const roleLabel = (value: string) => t(value === 'editor' ? 'com_e2_editor' : 'com_e2_viewer');
  const statusLabel = (value: string) =>
    t(
      value === 'accepted'
        ? 'com_e2_accepted'
        : value === 'revoked'
          ? 'com_e2_revoked'
          : value === 'expired'
            ? 'com_e2_expired'
            : 'com_e2_pending',
    );

  if (!available)
    return inbox.isError && !(inbox.error instanceof RequestError && inbox.error.status === 404) ? (
      <p role="alert">{t('com_enterprise_unavailable')}</p>
    ) : null;

  return (
    <div className="space-y-4" aria-busy={busy}>
      {failure && <p role="alert">{failure}</p>}
      {Boolean(inbox.data?.length) && (
        <section aria-label={t('com_e2_invitations')} className="space-y-2">
          <h3 className="font-semibold">{t('com_e2_invitations')}</h3>
          {inbox.data?.map((item) => (
            <div key={item.id} className="flex flex-wrap items-center justify-between gap-2">
              <span>
                {item.enterprise_name} · {roleLabel(item.role)}
              </span>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await request(`invitations/${item.id}/accept`, {});
                  })
                }
              >
                {t('com_e2_accept')}
              </Button>
            </div>
          ))}
        </section>
      )}
      {owner && root && (
        <section
          aria-label={t('com_e2_members')}
          className="border-border-light space-y-2 border-t pt-3"
        >
          <h3 className="font-semibold">{t('com_e2_members')}</h3>
          {canWrite && (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  await request(`${root}/invite`, { username: username.trim(), role });
                  setUsername('');
                });
              }}
            >
              <Input
                className="min-w-32 flex-1"
                aria-label={t('com_e2_username')}
                placeholder={t('com_e2_username')}
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
              <select
                className="bg-surface-primary rounded border p-2"
                aria-label={t('com_e2_role')}
                value={role}
                onChange={(e) => setRole(e.target.value)}
              >
                <option value="viewer">{roleLabel('viewer')}</option>
                <option value="editor">{roleLabel('editor')}</option>
              </select>
              <Button type="submit" disabled={busy}>
                {t('com_e2_invite')}
              </Button>
            </form>
          )}
          {members.isError && <p role="alert">{t('com_enterprise_unavailable')}</p>}
          {members.data?.map((item) => (
            <div key={item.id} className="flex flex-wrap items-center justify-between gap-2">
              <span>
                {item.username} · {roleLabel(item.role)} · {statusLabel(item.status)}
              </span>
              {(item.status === 'accepted' || item.status === 'pending') && (
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    if (!window.confirm(t('com_e2_confirm_revoke', { 0: item.username }))) return;
                    void run(async () => {
                      await request(`${root}/invitations/${item.id}/revoke`, {});
                    });
                  }}
                >
                  {t(item.status === 'pending' ? 'com_e2_cancel_invite' : 'com_e2_revoke')}
                </Button>
              )}
            </div>
          ))}
        </section>
      )}
      {root && (
        <section
          aria-label={t('com_e2_files')}
          className="border-border-light space-y-2 border-t pt-3"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-semibold">{t('com_e2_files')}</h3>
            <Button variant="ghost" disabled={busy} onClick={() => void run(async () => {})}>
              {t('com_enterprise_refresh')}
            </Button>
          </div>
          {canWrite && (
            <label className="block">
              {t('com_e2_upload')}
              <input
                className="block max-w-full"
                type="file"
                disabled={busy}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) save({ file, requestId: crypto.randomUUID() });
                  e.target.value = '';
                }}
              />
            </label>
          )}
          {files.isError && <p role="alert">{t('com_enterprise_unavailable')}</p>}
          {files.data?.length === 0 && <p>{t('com_e2_no_files')}</p>}
          {files.data?.map((file) => (
            <div key={file.id} className="flex flex-wrap items-center justify-between gap-2">
              <button
                type="button"
                className="text-left break-all underline-offset-2 hover:underline"
                onClick={() => setAsset(file)}
              >
                {file.name} · v{file.head}
              </button>
              <Button variant="outline" disabled={busy} onClick={() => download(file, file.head)}>
                {t('com_ui_download')}
              </Button>
            </div>
          ))}
          {asset && files.data?.some((file) => file.id === asset.id) && (
            <div className="border-border-light space-y-2 rounded-lg border p-2">
              <h4 className="font-medium">{t('com_e2_history', { 0: asset.name })}</h4>
              {canWrite && (
                <label className="block">
                  {t('com_e2_new_version')}
                  <input
                    className="block max-w-full"
                    type="file"
                    disabled={busy}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) save({ file, asset, requestId: crypto.randomUUID() });
                      e.target.value = '';
                    }}
                  />
                </label>
              )}
              {versions.isError && <p role="alert">{t('com_enterprise_unavailable')}</p>}
              {versions.data?.map((version) => (
                <Button
                  key={version.number}
                  aria-label={`${t('com_ui_download')} v${version.number}`}
                  variant="outline"
                  disabled={busy}
                  onClick={() => download(asset, version.number)}
                >
                  v{version.number} · {version.size} B
                </Button>
              ))}
            </div>
          )}
          {canWrite && pending && !busy && (
            <Button variant="outline" onClick={() => save(pending)}>
              {t('com_e2_retry_save')}
            </Button>
          )}
        </section>
      )}
    </div>
  );
}
