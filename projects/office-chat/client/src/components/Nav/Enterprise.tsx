import { useMemo } from 'react';
import { atom, useAtom } from 'jotai';
import { apiBaseUrl } from 'librechat-data-provider';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  Input,
  OGDialog,
  OGDialogContent,
  OGDialogHeader,
  OGDialogTitle,
} from '@librechat/client';
import type { RefObject } from 'react';
import { useAuthContext, useLocalize } from '~/hooks';
import EnterpriseCollaboration from './EnterpriseCollaboration';

type Overview = {
  applications: {
    id: string;
    name: string;
    status: 'pending' | 'issued' | 'revoked' | 'activated';
  }[];
  enterprises: {
    id: string;
    name: string;
    role: string;
    status: 'trial' | 'expired';
    setup_step: number;
    expires_at: string;
    limits: { seats: number; storage_gib: number; compute_minutes: number };
  }[];
};

type Action =
  | { path: 'apply'; body: { name: string } }
  | { path: 'activate'; body: { code: string } }
  | { path: `${string}/setup`; body: { step: number } };

async function request<T>(
  token: string | undefined,
  path: string,
  body?: Action['body'],
): Promise<T> {
  if (!token) throw new Error('401');
  const response = await fetch(`${apiBaseUrl()}/api/enterprise/${path}`, {
    method: body ? 'POST' : 'GET',
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    credentials: 'same-origin',
    cache: 'no-store',
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(String(response.status));
  return response.json() as Promise<T>;
}

const steps = [
  'com_enterprise_information',
  'com_enterprise_administrators',
  'com_enterprise_members',
  'com_enterprise_assets',
  'com_enterprise_budget',
] as const;

export default function Enterprise({
  onOpenChange,
  triggerRef,
}: {
  onOpenChange: (open: boolean) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const t = useLocalize();
  const { token, user } = useAuthContext();
  const queryClient = useQueryClient();
  const key = ['enterprise-overview', user?.id];
  const [form, setForm] = useAtom(useMemo(() => atom({ name: '', code: '', selected: '' }), []));
  const overview = useQuery(key, () => request<Overview>(token, 'overview'), {
    enabled: !!token && !!user?.id,
    retry: false,
    cacheTime: 0,
  });
  const action = useMutation({
    mutationFn: ({ path, body }: Action) => request(token, path, body),
    onSuccess: async (_, submitted) => {
      if (submitted.path === 'apply') setForm((previous) => ({ ...previous, name: '' }));
      if (submitted.path === 'activate') setForm((previous) => ({ ...previous, code: '' }));
      await queryClient.invalidateQueries(key);
    },
    retry: false,
    cacheTime: 0,
  });
  const spaces = overview.data?.enterprises ?? [];
  const enterprise = spaces.find((space) => space.id === form.selected) ?? spaces[0];
  const applicationStatus = {
    pending: 'com_enterprise_pending',
    issued: 'com_enterprise_issued',
    revoked: 'com_enterprise_revoked',
    activated: 'com_enterprise_activated',
  } as const;

  return (
    <OGDialog open onOpenChange={onOpenChange} triggerRef={triggerRef}>
      <OGDialogContent className="max-h-[90dvh] w-11/12 max-w-lg overflow-y-auto">
        <OGDialogHeader>
          <OGDialogTitle>{t('com_enterprise_title')}</OGDialogTitle>
        </OGDialogHeader>
        <div className="space-y-5 text-sm" aria-busy={overview.isFetching || action.isLoading}>
          {overview.isLoading && <p role="status">{t('com_starbridge_loading')}</p>}
          {overview.isError && (
            <div role="alert">
              <p>{t('com_enterprise_unavailable')}</p>
              <Button variant="outline" onClick={() => void overview.refetch()}>
                {t('com_enterprise_refresh')}
              </Button>
            </div>
          )}
          {action.isError && (
            <p role="alert">
              {action.error instanceof Error &&
              action.error.message === '404' &&
              action.variables?.path === 'activate'
                ? t('com_enterprise_bad_code')
                : t('com_enterprise_failed')}
            </p>
          )}
          {action.isSuccess && <p role="status">{t('com_enterprise_saved')}</p>}
          {overview.data && (
            <>
              <EnterpriseCollaboration key={enterprise?.id ?? 'inbox'} enterprise={enterprise} />
              {enterprise && (
                <section className="border-border-light space-y-2 rounded-xl border p-3">
                  {spaces.length > 1 ? (
                    <label className="flex flex-wrap items-center gap-2">
                      {t('com_enterprise_title')}
                      <select
                        className="bg-surface-primary max-w-full rounded border px-2 py-1"
                        value={enterprise.id}
                        onChange={(event) => setForm({ ...form, selected: event.target.value })}
                      >
                        {spaces.map((space) => (
                          <option key={space.id} value={space.id}>
                            {space.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    <h3 className="font-semibold break-words">{enterprise.name}</h3>
                  )}
                  <p className="text-text-secondary">
                    {enterprise.role === 'owner' && `${t('com_enterprise_owner')} · `}
                    {t(
                      enterprise.status === 'trial'
                        ? 'com_enterprise_trial'
                        : 'com_enterprise_expired',
                    )}
                    {' · '}
                    {new Date(enterprise.expires_at).toLocaleDateString()}
                  </p>
                  <p>
                    {t('com_enterprise_limits', {
                      0: enterprise.limits.seats,
                      1: enterprise.limits.storage_gib,
                      2: enterprise.limits.compute_minutes,
                    })}
                  </p>
                  <ol className="space-y-1">
                    {steps.map((step, index) => (
                      <li key={step} className="flex justify-between gap-2">
                        {t(step)}
                        {index < enterprise.setup_step && (
                          <span>{t('com_enterprise_reviewed')}</span>
                        )}
                      </li>
                    ))}
                  </ol>
                  {enterprise.role === 'owner' &&
                    enterprise.status === 'trial' &&
                    enterprise.setup_step < steps.length && (
                      <Button
                        variant="outline"
                        disabled={action.isLoading}
                        onClick={() =>
                          action.mutate({
                            path: `${enterprise.id}/setup`,
                            body: { step: enterprise.setup_step + 1 },
                          })
                        }
                      >
                        {t('com_enterprise_review_next', { 0: t(steps[enterprise.setup_step]) })}
                      </Button>
                    )}
                </section>
              )}
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  action.mutate({ path: 'apply', body: { name: form.name.trim() } });
                }}
              >
                <label className="block" htmlFor="enterprise-name">
                  {t('com_enterprise_name')}
                </label>
                <Input
                  id="enterprise-name"
                  required
                  maxLength={120}
                  value={form.name}
                  onChange={(event) => setForm({ ...form, name: event.target.value })}
                />
                <Button type="submit" disabled={action.isLoading || form.name.trim().length < 2}>
                  {t('com_enterprise_apply')}
                </Button>
              </form>
              {overview.data.applications.length > 0 && (
                <ul className="space-y-1">
                  {overview.data.applications.map((row) => (
                    <li key={row.id} className="flex justify-between gap-2">
                      <span className="break-all">{row.name}</span>
                      <span className="text-text-secondary shrink-0">
                        {t(applicationStatus[row.status])}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  action.mutate({ path: 'activate', body: { code: form.code.trim() } });
                }}
              >
                <label className="block" htmlFor="enterprise-code">
                  {t('com_enterprise_code')}
                </label>
                <Input
                  id="enterprise-code"
                  type="password"
                  autoComplete="off"
                  required
                  maxLength={80}
                  value={form.code}
                  onChange={(event) => setForm({ ...form, code: event.target.value })}
                />
                <Button type="submit" disabled={action.isLoading || form.code.trim().length !== 43}>
                  {t('com_enterprise_activate')}
                </Button>
              </form>
            </>
          )}
        </div>
      </OGDialogContent>
    </OGDialog>
  );
}
