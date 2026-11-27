import { useMemo, useRef } from 'react';
import { atom, useAtom } from 'jotai';
import {
  Folder,
  Download,
  Upload,
  FolderPlus,
  ArrowLeft,
  RefreshCw,
  File,
  History,
} from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@librechat/client';
import {
  personalFileRequest,
  validPersonalFileName,
  PersonalFileError,
  QueryKeys,
} from 'librechat-data-provider';
import type { PersonalFileListing } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import Versions from './Versions';

export function personalError(error: unknown) {
  if (error instanceof PersonalFileError && error.status === 409)
    return 'com_starbridge_files_conflict' as const;
  if (error instanceof PersonalFileError && error.status === 507)
    return 'com_starbridge_files_full' as const;
  if (error instanceof PersonalFileError && error.status === 413)
    return 'com_starbridge_files_large' as const;
  return 'com_starbridge_files_error' as const;
}

export default function PersonalFiles() {
  const t = useLocalize();
  const { user, token } = useAuthContext();
  const client = useQueryClient();
  const upload = useRef<HTMLInputElement>(null);
  const [view, setView] = useAtom(
    useMemo(() => atom({ path: '', creating: false, name: '', history: '' }), []),
  );
  const listing = useQuery({
    queryKey: [QueryKeys.personalFiles, user?.id, view.path],
    queryFn: async (): Promise<PersonalFileListing> =>
      (await personalFileRequest(token!, 'directories', 'GET', view.path)).json(),
    enabled: !!token,
    retry: false,
  });
  const action = useMutation({
    mutationKey: [QueryKeys.personalFiles, user?.id, 'write'],
    mutationFn: async ({ file, name }: { file?: Blob; name: string }) => {
      if (!validPersonalFileName(name)) throw new Error('invalid_name');
      if (file && file.size > 16 * 1024 * 1024) throw new PersonalFileError(413);
      const path = view.path ? `${view.path}/${name}` : name;
      await personalFileRequest(
        token!,
        file ? 'file' : 'directories',
        file ? 'PUT' : 'POST',
        path,
        file,
      );
    },
    onSuccess: async () => {
      setView((old) => ({ ...old, creating: false, name: '' }));
      await client.invalidateQueries([QueryKeys.personalFiles, user?.id]);
    },
    retry: false,
  });
  const download = useMutation({
    mutationFn: async (name: string) => {
      const path = view.path ? `${view.path}/${name}` : name;
      const blob = await (await personalFileRequest(token!, 'file', 'GET', path)).blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    },
    retry: false,
  });
  const navigate = (path: string) => {
    action.reset();
    download.reset();
    setView({ path, creating: false, name: '', history: '' });
  };
  if (view.history)
    return <Versions path={view.history} onBack={() => setView({ ...view, history: '' })} />;
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => upload.current?.click()}
          disabled={action.isLoading}
        >
          <Upload className="size-4" />
          {t('com_starbridge_files_upload')}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setView({ ...view, creating: !view.creating })}
          disabled={action.isLoading}
        >
          <FolderPlus className="size-4" />
          {t('com_starbridge_files_folder')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={t('com_starbridge_refresh')}
          onClick={() => listing.refetch()}
          disabled={listing.isFetching}
        >
          <RefreshCw className="size-4" />
        </Button>
        <input
          ref={upload}
          type="file"
          className="hidden"
          aria-label={t('com_starbridge_files_upload')}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) action.mutate({ file, name: file.name });
            e.target.value = '';
          }}
        />
      </div>
      {view.creating && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            action.mutate({ name: view.name });
          }}
        >
          <input
            className="border-border-light bg-surface-primary min-w-0 flex-1 rounded-lg border px-3 py-2"
            aria-label={t('com_starbridge_files_name')}
            value={view.name}
            onChange={(e) => setView({ ...view, name: e.target.value })}
          />
          <Button
            type="submit"
            size="sm"
            disabled={action.isLoading || !validPersonalFileName(view.name)}
          >
            {t('com_ui_create')}
          </Button>
        </form>
      )}
      <div className="flex items-center gap-2">
        {view.path && (
          <Button
            variant="ghost"
            size="sm"
            aria-label={t('com_starbridge_files_parent')}
            disabled={action.isLoading}
            onClick={() => navigate(view.path.split('/').slice(0, -1).join('/'))}
          >
            <ArrowLeft className="size-4" />
          </Button>
        )}
        <span className="min-w-0 break-all">
          {t('com_starbridge_files_personal')}
          {view.path ? ` / ${view.path}` : ''}
        </span>
      </div>
      {(listing.isError || listing.isPaused || action.isError || download.isError) && (
        <p role="alert" className="text-text-secondary">
          {t(personalError(action.error ?? download.error ?? listing.error))}
        </p>
      )}
      {action.isSuccess && <p role="status">{t('com_starbridge_files_saved')}</p>}
      {(listing.isFetching || action.isLoading) && (
        <p role="status">{t('com_starbridge_loading')}</p>
      )}
      <div className="border-border-light max-h-[50vh] overflow-y-auto rounded-lg border">
        {listing.data?.entries.map((entry) => (
          <div
            key={entry.name}
            className="border-border-light flex items-center gap-3 border-b px-3 py-2 last:border-0"
          >
            {entry.kind === 'directory' ? (
              <Folder className="size-4 shrink-0" />
            ) : (
              <File className="size-4 shrink-0" />
            )}
            {entry.kind === 'directory' ? (
              <button
                className="min-w-0 flex-1 truncate text-start hover:underline"
                disabled={action.isLoading}
                onClick={() => navigate(view.path ? `${view.path}/${entry.name}` : entry.name)}
              >
                {entry.name}
              </button>
            ) : (
              <span className="min-w-0 flex-1 truncate" title={entry.name}>
                {entry.name}
              </span>
            )}
            {entry.kind === 'file' && (
              <>
                <span className="text-text-secondary text-xs tabular-nums">
                  {((entry.size ?? 0) / 1024).toFixed(1)} KB
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`${t('com_starbridge_versions')} ${entry.name}`}
                  onClick={() =>
                    setView({
                      ...view,
                      history: view.path ? `${view.path}/${entry.name}` : entry.name,
                    })
                  }
                >
                  <History className="size-4" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`${t('com_ui_download')} ${entry.name}`}
                  disabled={download.isLoading}
                  onClick={() => download.mutate(entry.name)}
                >
                  <Download className="size-4" />
                </Button>
              </>
            )}
          </div>
        ))}
        {listing.isSuccess && listing.data.entries.length === 0 && (
          <p className="text-text-secondary p-6 text-center">{t('com_starbridge_files_empty')}</p>
        )}
      </div>
    </div>
  );
}
