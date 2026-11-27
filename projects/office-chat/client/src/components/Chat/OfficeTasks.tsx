import { useMutation, useQuery } from '@tanstack/react-query';
import { QueryKeys, fetchOfficeTasks, cancelOfficeTask, apiBaseUrl } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import { useGetStartupConfig } from '~/data-provider';
import SavePersonal from './Input/Files/SavePersonal';
import OfficeWordPreview from './OfficeWordPreview';

/** Reconnect only reads receipts. It never resubmits the chat or code. */
export default function OfficeTasks({
  conversationId,
  active,
  onOpenFile,
}: {
  conversationId?: string | null;
  active: boolean;
  onOpenFile?: () => void;
}) {
  const { data: config } = useGetStartupConfig();
  const { user, token } = useAuthContext();
  const t = useLocalize();
  const tasks = useQuery({
    queryKey: [QueryKeys.officeTasks, user?.id, conversationId],
    queryFn: () => fetchOfficeTasks(token!, conversationId!),
    enabled: !!config?.officeTasks && !!token && !!conversationId && conversationId !== 'new',
    refetchInterval: (data) =>
      active ||
      data?.some(
        (task) =>
          task.state !== 'complete' &&
          task.state !== 'cancelled' &&
          Date.now() - Date.parse(task.created_at) < 10 * 60 * 1000,
      )
        ? 4000
        : data?.some((task) => task.output_expires_at && !task.outputs_expired)
          ? 60000
          : false,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const cancellation = useMutation({
    mutationFn: (id: string) => cancelOfficeTask(token!, id),
    onSettled: () => void tasks.refetch(),
    retry: false,
  });
  if (!config?.officeTasks) return null;
  return (
    <div className="text-text-primary min-h-0 overflow-y-auto text-sm">
      {tasks.isLoading && <p role="status">{t('com_ui_loading')}</p>}
      {tasks.isSuccess && !tasks.data.length && <p>{t('com_office_tasks_empty')}</p>}
      <button
        type="button"
        className="text-text-secondary mt-2 underline"
        disabled={tasks.isFetching}
        onClick={() => void tasks.refetch()}
      >
        {t('com_ui_refresh')}
      </button>
      {tasks.isError && <p role="alert">{t('com_office_tasks_unavailable')}</p>}
      {tasks.data?.map((task) => {
        let statusKey:
          | 'com_office_task_complete'
          | 'com_office_task_unknown'
          | 'com_office_task_cancelled'
          | 'com_office_task_cancelling'
          | 'com_office_task_running' = 'com_office_task_running';
        if (task.state === 'complete') statusKey = 'com_office_task_complete';
        if (task.state === 'unknown') statusKey = 'com_office_task_unknown';
        if (task.state === 'cancelled') statusKey = 'com_office_task_cancelled';
        if (task.state === 'cancelling') statusKey = 'com_office_task_cancelling';
        const canCancel =
          ['accepted', 'running', 'unknown'].includes(task.state) &&
          Date.now() - Date.parse(task.created_at) < 6 * 24 * 3600 * 1000;
        const result = task.result;
        const files = (result?.files ?? []).flatMap((file) => {
          const session = file.storage_session_id ?? file.session_id ?? result?.session_id;
          if (!session || !/^[\w-]+$/.test(session) || !/^[\w-]+$/.test(file.id)) return [];
          return [
            {
              filename: file.name,
              filepath: `${apiBaseUrl()}/api/files/code/download/${session}/${file.id}`,
              task: { taskId: task.id, fileId: file.id, expired: !!task.outputs_expired },
            },
          ];
        });
        return (
          <div
            key={task.id}
            id={`file-task-${task.id}`}
            className="border-border-light mt-2 border-t py-2"
          >
            <p role="status">{t(statusKey)}</p>
            {canCancel && (
              <button
                type="button"
                className="text-text-secondary mt-1 underline"
                disabled={cancellation.isLoading}
                onClick={() => cancellation.mutate(task.id)}
              >
                {t('com_office_task_cancel')}
              </button>
            )}
            {cancellation.isError && cancellation.variables === task.id && (
              <p role="alert">{t('com_office_tasks_unavailable')}</p>
            )}
            {!!task.inputs?.length && (
              <details className="mt-1">
                <summary className="text-text-secondary cursor-pointer">
                  {t('com_office_task_inputs')}
                </summary>
                <ul>
                  {task.inputs.map((file, index) => (
                    <li
                      key={`${file.storage_session_id}-${file.id}-${index}`}
                      className="break-all"
                    >
                      {file.name}
                      {file.sha256 ? ` · ${file.sha256.slice(0, 12)}` : ''}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {task.outputs_expired && <p>{t('com_office_output_expired')}</p>}
            {result?.stdout && (
              <details>
                <summary className="text-text-secondary cursor-pointer">{t('com_office_task_details')}</summary>
                <pre className="max-h-48 overflow-auto break-words whitespace-pre-wrap">
                  {result.stdout.slice(0, 8000)}
                </pre>
              </details>
            )}
            {!!result?.stderr && <p>{t('com_office_task_diagnostic')}</p>}
            {files.map((file) => (
              <div key={file.filepath} className="flex flex-wrap items-baseline gap-x-3">
                <span className="break-all">{file.filename}</span>
                {!file.task.expired && /\.docx$/i.test(file.filename) && (
                  <OfficeWordPreview filename={file.filename} filepath={file.filepath} onOpen={onOpenFile} />
                )}
              </div>
            ))}
            <SavePersonal files={files} />
          </div>
        );
      })}
    </div>
  );
}
