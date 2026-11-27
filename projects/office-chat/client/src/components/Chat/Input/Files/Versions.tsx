import { ArrowLeft, Download } from 'lucide-react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Button } from '@librechat/client';
import { personalFileRequest, QueryKeys } from 'librechat-data-provider';
import type { PersonalHistory } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';

export function usePersonalHistory(path: string) {
  const { user, token } = useAuthContext();
  return useQuery({
    queryKey: [QueryKeys.personalFiles, user?.id, 'revisions', path],
    queryFn: async (): Promise<PersonalHistory> =>
      (await personalFileRequest(token!, 'revisions', 'GET', path)).json(),
    enabled: !!token && !!path,
    retry: false,
  });
}

export default function Versions({ path, onBack }: { path: string; onBack: () => void }) {
  const t = useLocalize();
  const { token } = useAuthContext();
  const history = usePersonalHistory(path);
  const name = path.split('/').pop() ?? '';
  const download = useMutation({
    mutationFn: async (version: number) => {
      const blob = await (
        await personalFileRequest(token!, 'revisions', 'GET', path, undefined, { version })
      ).blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const dot = name.lastIndexOf('.');
      a.download =
        dot > 0 ? `${name.slice(0, dot)}-v${version}${name.slice(dot)}` : `${name}-v${version}`;
      a.href = url;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    },
    retry: false,
  });
  return (
    <div className="space-y-3 text-sm">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" aria-label={t('com_ui_back')} onClick={onBack}>
          <ArrowLeft className="size-4" />
        </Button>
        <span className="min-w-0 break-all">
          {name} · {t('com_starbridge_versions')}
        </span>
      </div>
      {history.isFetching && <p role="status">{t('com_starbridge_loading')}</p>}
      {(history.isError || history.isPaused || download.isError) && (
        <p role="alert">{t('com_starbridge_files_error')}</p>
      )}
      {history.isSuccess && !history.data.revisions.length && (
        <p>{t('com_starbridge_versions_original')}</p>
      )}
      <div className="max-h-[50dvh] overflow-y-auto">
        {history.data?.revisions.map((v) => (
          <div
            key={v.version}
            className="border-border-light flex items-center gap-3 border-b py-3"
          >
            <span className="font-medium">{`v${v.version}`}</span>
            <span className="text-text-secondary min-w-0 flex-1 text-xs">
              {new Date(v.savedAt).toLocaleString()} · {(v.size / 1024).toFixed(1)} KB
              {v.source && (
                <span className="block break-all">
                  {t('com_office_source_task')} {v.source.taskId}
                </span>
              )}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={download.isLoading}
              aria-label={`${t('com_ui_download')} v${v.version}`}
              onClick={() => download.mutate(v.version)}
            >
              <Download className="size-4" />
            </Button>
          </div>
        ))}
      </div>
    </div>
  );
}
