import { useMemo, useRef, useCallback } from 'react';
import { atom, useAtom } from 'jotai';
import { FolderDown } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  OGDialog,
  OGDialogContent,
  OGDialogTitle,
  OGDialogDescription,
} from '@librechat/client';
import {
  dataService,
  personalFileRequest,
  validPersonalFileName,
  PersonalFileError,
  QueryKeys,
  personalSaveIdentity,
} from 'librechat-data-provider';
import type { TAttachment } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import { useGetStartupConfig } from '~/data-provider';
import { personalError } from './PersonalFiles';
import PersonalBrowser from './PersonalBrowser';
import { usePersonalHistory } from './Versions';
import { useShareContext } from '~/Providers';
import { compareWordText } from '../../OfficeWordPreview';

type FileResult = Partial<TAttachment> & {
  user?: string;
  file_id?: string;
  task?: { taskId: string; fileId: string; expired: boolean };
  load?: () => Promise<Blob>;
};

async function readOutput(file: FileResult, owner?: string) {
  const href = file.filepath ?? '';
  let blob: unknown;
  if (file.load) blob = await file.load();
  else if (href.startsWith('/api/files/code/') || href.startsWith('/api/files/code?'))
    blob = (await dataService.getCodeOutputDownload(href)).data;
  else if (file.file_id && owner) blob = (await dataService.getFileDownload(owner, file.file_id)).data;
  if (!(blob instanceof Blob)) throw new Error('download_unavailable');
  if (blob.size > 16 * 1024 * 1024) throw new PersonalFileError(413);
  return blob;
}

export default function SavePersonal({ files }: { files: FileResult[] }) {
  const { data } = useGetStartupConfig();
  const { shareId } = useShareContext();
  const { user } = useAuthContext();
  const eligible = files.filter(
    (file) =>
      file?.filename &&
      (file.filepath || (file.load && file.file_id)) &&
      (!file.user || file.user === user?.id),
  );
  if (!data?.personalFiles || shareId || !user || !eligible.length) return null;
  return <SaveFiles files={eligible} />;
}

function SaveFiles({ files }: { files: FileResult[] }) {
  const t = useLocalize();
  const { user, token } = useAuthContext();
  const client = useQueryClient();
  const trigger = useRef<HTMLButtonElement>(null);
  const attempt = useRef<{ requestId: string; version: number }>();
  const [view, setView] = useAtom(
    useMemo(
      () =>
        atom({
          open: false,
          selected: 0,
          name: '',
          path: '',
          ready: false,
          revision: false,
          base: '',
          requestId: '',
        }),
      [],
    ),
  );
  const history = usePersonalHistory(view.open && view.revision ? view.base : '');
  const selectedTask = files[view.selected]?.task;
  const priorTaskVersion = history.data?.revisions.find(
    (entry) =>
      !!selectedTask &&
      entry.source?.taskId === selectedTask.taskId &&
      entry.source?.fileId === selectedTask.fileId,
  )?.version;
  const outputAvailable =
    !selectedTask?.expired || (view.revision && priorTaskVersion !== undefined);
  const nextVersion = Math.max(1, ...(history.data?.revisions.map((v) => v.version) ?? [])) + 1;
  const revisionReady =
    !view.revision ||
    (!!view.base && history.isSuccess && !history.isFetching && !history.isPaused);
  const output = files[view.selected];
  const reviewWord = !!(view.open && view.revision && view.base && /\.docx$/i.test(output?.filename ?? '') &&
    !priorTaskVersion && !selectedTask?.expired);
  const audience = useQuery({
    queryKey: ['word-review-availability', user?.id],
    queryFn: async () => (await personalFileRequest(token!, 'review-status')).json() as Promise<{ enabled: boolean; sections?: boolean }>,
    enabled: reviewWord && !!token,
    retry: false,
  });
  const review = useQuery({
    queryKey: ['word-change-review', user?.id, view.base, nextVersion, output?.file_id, output?.filepath, !!audience.data?.sections],
    enabled: reviewWord && !!audience.data?.enabled && !!token && revisionReady,
    queryFn: async () => {
      const previous = nextVersion - 1;
      const [base, candidate] = await Promise.all([
        personalFileRequest(token!, previous > 1 ? 'revisions' : 'file', 'GET', view.base, undefined,
          previous > 1 ? { version: previous } : undefined).then(r => r.blob()),
        readOutput(output, user?.id),
      ]);
      return compareWordText(base, candidate, !!audience.data?.sections);
    },
    retry: false,
    cacheTime: 0,
  });
  const folderReady = useCallback(
    (ready: boolean) => setView((old) => (old.ready === ready ? old : { ...old, ready })),
    [setView],
  );
  const save = useMutation({
    mutationKey: [QueryKeys.personalFiles, user?.id, 'save-result'],
    mutationFn: async () => {
      if (reviewWord && audience.data?.enabled && review.data?.some(change => change.calculation === 'mismatch'))
        throw new Error('word_formula_mismatch');
      const file = files[view.selected];
      if (!file || !token || !view.ready || !revisionReady || !validPersonalFileName(view.name))
        throw new Error('invalid_file');
      if (view.revision && file.task) {
        return (
          await personalFileRequest(token, 'task-revision', 'POST', view.base, undefined, {
            ...file.task,
            version: nextVersion,
          })
        ).json();
      }
      const href = file.filepath ?? '';
      const blob = await readOutput(file, user?.id);
      const path = view.path ? `${view.path}/${view.name}` : view.name;
      if (view.revision) {
        const requestId = await personalSaveIdentity(user!.id, file.file_id ?? href, view.base);
        const previous = history.data?.revisions.find(
          (revision) => revision.requestId === requestId,
        );
        if (attempt.current?.requestId !== requestId)
          attempt.current = { requestId, version: previous?.version ?? nextVersion };
        return (
          await personalFileRequest(token, 'revisions', 'POST', view.base, blob, attempt.current)
        ).json();
      }
      await personalFileRequest(token, 'file', 'PUT', path, blob);
      return { version: 1 };
    },
    onSuccess: () => client.invalidateQueries([QueryKeys.personalFiles, user?.id]),
    retry: false,
  });
  return (
    <>
      <Button
        ref={trigger}
        variant="outline"
        size="sm"
        className="my-2"
        onClick={() => {
          save.reset();
          setView({
            open: true,
            selected: 0,
            name: files[0].filename ?? '',
            path: '',
            ready: false,
            revision: !!files[0].task?.expired,
            base: '',
            requestId: crypto.randomUUID(),
          });
        }}
      >
        <FolderDown className="size-4" />
        {t('com_starbridge_files_save')}
      </Button>
      <OGDialog
        open={view.open}
        triggerRef={trigger}
        onOpenChange={(open) => {
          if (!save.isLoading) setView({ ...view, open });
        }}
      >
        <OGDialogContent className="max-h-[90dvh] w-[calc(100%-2rem)] max-w-md overflow-y-auto">
          <OGDialogTitle>{t('com_starbridge_files_save')}</OGDialogTitle>
          <OGDialogDescription>
            {t(view.revision ? 'com_starbridge_versions_help' : 'com_starbridge_files_save_help')}
          </OGDialogDescription>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={view.revision}
              disabled={save.isLoading}
              onChange={(e) => {
                save.reset();
                setView({
                  ...view,
                  revision: e.target.checked,
                  base: '',
                  requestId: crypto.randomUUID(),
                });
              }}
            />
            {t('com_starbridge_versions_save')}
          </label>
          {files.length > 1 && (
            <select
              aria-label={t('com_starbridge_files_result')}
              className="border-border-light bg-surface-primary w-full rounded-lg border p-2"
              value={view.selected}
              disabled={save.isLoading}
              onChange={(e) => {
                const selected = Number(e.target.value);
                save.reset();
                setView({
                  ...view,
                  selected,
                  name: files[selected].filename ?? '',
                  base: '',
                  requestId: crypto.randomUUID(),
                });
              }}
            >
              {files.map((file, index) => (
                <option key={index} value={index}>
                  {file.filename}
                </option>
              ))}
            </select>
          )}
          {!view.revision && (
            <label className="flex flex-col gap-2 text-sm">
              {t('com_starbridge_files_name')}
              <input
                className="border-border-light bg-surface-primary rounded-lg border px-3 py-2"
                value={view.name}
                disabled={save.isLoading}
                onChange={(e) => {
                  save.reset();
                  setView({ ...view, name: e.target.value });
                }}
              />
            </label>
          )}
          <div className="space-y-2">
            <span className="text-sm">
              {t(
                view.revision
                  ? 'com_starbridge_versions_target'
                  : 'com_starbridge_files_destination',
              )}
            </span>
            <PersonalBrowser
              path={view.path}
              disabled={save.isLoading}
              onReady={folderReady}
              selected={view.base}
              extension={view.revision ? view.name.split('.').pop()?.toLowerCase() : undefined}
              onFile={
                view.revision
                  ? (base, name) => {
                      save.reset();
                      if (
                        name.split('.').pop()?.toLowerCase() !==
                        view.name.split('.').pop()?.toLowerCase()
                      )
                        return;
                      setView({ ...view, base, requestId: crypto.randomUUID() });
                    }
                  : undefined
              }
              onNavigate={(path) => {
                save.reset();
                setView({ ...view, path, ready: false, base: '' });
              }}
            />
          </div>
          {view.revision && view.base && (
            <p className="text-sm break-all">
              {`${view.base} · v${save.isSuccess ? save.data?.version : (priorTaskVersion ?? nextVersion)}`}
            </p>
          )}
          {reviewWord && audience.data?.enabled && !save.isSuccess && (
            <div aria-live="polite" className="max-h-[30dvh] space-y-2 overflow-auto text-sm">
              {review.isLoading && <p>{t('com_ui_loading')}</p>}
              {review.isError && <p>{t('com_office_compare_manual')}</p>}
              {review.data && <p>{t('com_office_compare_count', { 0: review.data.length })}</p>}
              {review.data?.map(c => <div key={c.paragraph} className="border-border-light border-t pt-1">
                {c.section && <p className="break-words font-medium">{c.section}</p>}
                <p>{c.table ? t('com_office_compare_cell', { 0: c.table.index, 1: c.table.row, 2: c.table.column })
                  : t('com_office_compare_paragraph', { 0: c.paragraph })}</p>
                <div className="break-words"><del>{c.before}</del></div>
                <div className="break-words"><ins>{c.after}</ins></div>
                {c.calculation && <p>{t(`com_office_compare_math_${c.calculation}`)}</p>}
              </div>)}
            </div>
          )}
          {view.revision && (history.isError || history.isPaused) && (
            <p role="alert">{t('com_starbridge_files_error')}</p>
          )}
          {save.isError && (
            <p role="alert" className="text-sm">
              {t(
                save.error instanceof PersonalFileError && save.error.status === 410
                  ? 'com_office_output_expired'
                  : personalError(save.error),
              )}
            </p>
          )}
          {save.isSuccess && (
            <p role="status" className="text-sm">
              {t('com_starbridge_files_saved')}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={save.isLoading}
              onClick={() => setView({ ...view, open: false })}
            >
              {t(save.isSuccess ? 'com_ui_close' : 'com_ui_cancel')}
            </Button>
            {!save.isSuccess && (
              <Button
                disabled={
                  save.isLoading ||
                  !view.ready ||
                  !revisionReady ||
                  (reviewWord && (audience.isLoading || (audience.data?.enabled &&
                    (review.isLoading || review.data?.some(change => change.calculation === 'mismatch'))))) ||
                  !outputAvailable ||
                  !validPersonalFileName(view.name)
                }
                onClick={() => save.mutate()}
              >
                {t(save.isLoading ? 'com_starbridge_loading' : 'com_ui_save')}
              </Button>
            )}
          </div>
        </OGDialogContent>
      </OGDialog>
    </>
  );
}
