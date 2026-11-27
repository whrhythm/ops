import { useEffect } from 'react';
import { ArrowLeft, File, Folder, RefreshCw } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@librechat/client';
import { personalFileRequest, QueryKeys } from 'librechat-data-provider';
import type { PersonalFileListing } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';

/** Shared directory browser. A selection is a path, never a user-supplied service URL. */
export default function PersonalBrowser({
  path,
  onNavigate,
  onFile,
  selected,
  disabled = false,
  onReady,
  extension,
}: {
  path: string;
  onNavigate: (path: string) => void;
  onFile?: (path: string, name: string) => void;
  selected?: string;
  disabled?: boolean;
  onReady?: (ready: boolean) => void;
  extension?: string | readonly string[];
}) {
  const t = useLocalize();
  const { user, token } = useAuthContext();
  const listing = useQuery({
    queryKey: [QueryKeys.personalFiles, user?.id, path],
    queryFn: async (): Promise<PersonalFileListing> =>
      (await personalFileRequest(token!, 'directories', 'GET', path)).json(),
    enabled: !!token,
    retry: false,
  });
  const ready = listing.isSuccess && !listing.isFetching && !listing.isPaused;
  useEffect(() => onReady?.(ready), [onReady, ready]);
  const entries = listing.data?.entries.filter(
    (entry) =>
      entry.kind === 'directory' ||
      (onFile && (!extension || (typeof extension === 'string' ? [extension] : extension)
        .includes(entry.name.split('.').pop()?.toLowerCase() ?? ''))),
  );
  return (
    <div className="min-w-0 space-y-2 text-sm">
      <div className="flex items-center gap-2">
        {path && (
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled}
            aria-label={t('com_starbridge_files_parent')}
            onClick={() => onNavigate(path.split('/').slice(0, -1).join('/'))}
          >
            <ArrowLeft className="size-4" />
          </Button>
        )}
        <span className="min-w-0 flex-1 break-all">
          {t('com_starbridge_files_personal')}
          {path ? ` / ${path}` : ''}
        </span>
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled || listing.isFetching}
          aria-label={t('com_starbridge_refresh')}
          onClick={() => listing.refetch()}
        >
          <RefreshCw className="size-4" />
        </Button>
      </div>
      {(listing.isError || listing.isPaused) && (
        <p role="alert">{t('com_starbridge_files_error')}</p>
      )}
      {listing.isFetching && <p role="status">{t('com_starbridge_loading')}</p>}
      <div className="border-border-light max-h-[25dvh] overflow-y-auto rounded-lg border">
        {entries?.map((entry) => {
          const fullPath = path ? `${path}/${entry.name}` : entry.name;
          return (
            <button
              key={entry.name}
              type="button"
              disabled={disabled || !ready}
              aria-pressed={entry.kind === 'file' ? selected === fullPath : undefined}
              className={`border-border-light hover:bg-surface-hover flex w-full items-center gap-2 border-b px-3 py-2 text-start last:border-0 ${selected === fullPath ? 'bg-surface-active-alt' : ''}`}
              onClick={() =>
                entry.kind === 'directory' ? onNavigate(fullPath) : onFile?.(fullPath, entry.name)
              }
            >
              {entry.kind === 'directory' ? (
                <Folder className="size-4 shrink-0" />
              ) : (
                <File className="size-4 shrink-0" />
              )}
              <span className="min-w-0 flex-1 truncate" title={entry.name}>
                {entry.name}
              </span>
            </button>
          );
        })}
        {ready && !entries?.length && (
          <p className="text-text-secondary p-3">
            {t(onFile ? 'com_starbridge_files_empty' : 'com_starbridge_files_no_folders')}
          </p>
        )}
      </div>
    </div>
  );
}
