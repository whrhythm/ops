import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { apiBaseUrl, fetchOfficeTasks, QueryKeys } from 'librechat-data-provider';
import type { OfficeTask } from 'librechat-data-provider';
import { useAuthContext, useLocalize } from '~/hooks';
import { useGetStartupConfig } from '~/data-provider';
import WordPages from './WordPages';

type CallPart = { tool_call?: { id?: string; name?: string } };

export default function WordDelivery({ conversation, parts, active }: {
  conversation?: string | null; parts?: readonly (CallPart | null | undefined)[]; active?: boolean;
}) {
  const ids = (parts ?? []).flatMap(part => part?.tool_call?.name === 'execute_code' &&
    part.tool_call.id ? [part.tool_call.id] : []);
  return conversation && ids.length ? <WordReceipt conversation={conversation} calls={JSON.stringify(ids)} active={active} /> : null;
}

function WordReceipt({ conversation, calls, active }: { conversation: string; calls: string; active?: boolean }) {
  const { token, user } = useAuthContext();
  const { data: config } = useGetStartupConfig();
  const localize = useLocalize();
  const [keys, setKeys] = useState<string[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    setKeys([]);
    Promise.all((JSON.parse(calls) as string[]).map(async id => {
      const input = new TextEncoder().encode(JSON.stringify([conversation, id]));
      const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
      return Array.from(hash, byte => byte.toString(16).padStart(2, '0')).join('');
    })).then(result => { if (current) setKeys(result); }).catch(() => {});
    return () => { current = false; };
  }, [conversation, calls]);
  const tasks = useQuery<OfficeTask[]>({
    queryKey: [QueryKeys.officeTasks, user?.id, conversation],
    queryFn: () => fetchOfficeTasks(token!, conversation),
    enabled: !!token && !!user && !!config?.officeTasks,
    staleTime: 3000, retry: false,
  });
  const wasActive = useRef(active);
  const { refetch } = tasks;
  useEffect(() => {
    if (wasActive.current && !active && token && config?.officeTasks) void refetch();
    wasActive.current = active;
  }, [active, refetch, token, config?.officeTasks]);
  // The recent list is shared by the task panel. Older replies look up only their own calls.
  const missing = tasks.data && tasks.data.length >= 50
    ? keys.filter(key => !tasks.data!.some(task => task.call_key === key)) : [];
  const history = useQuery<OfficeTask[]>({
    queryKey: [QueryKeys.officeTasks, user?.id, conversation, 'history', missing],
    enabled: !!user && !!token && !!config?.officeTasks && missing.length > 0,
    queryFn: async () => {
      const found: OfficeTask[] = [];
      for (let index = 0; index < missing.length; index += 16) {
        const query = new URLSearchParams({conversation_id: conversation});
        for (const key of missing.slice(index, index + 16)) query.append('call_key', key);
        const response = await fetch(apiBaseUrl() + '/api/files/office-tasks?' + query, {
          headers: {Authorization: 'Bearer ' + token}, cache: 'no-store', redirect: 'error',
        });
        if (!response.ok) throw Error('office_tasks_unavailable');
        found.push(...(await response.json()).tasks);
      }
      return found;
    },
    staleTime: 60000, retry: 1,
  });
  const files = new Map<string, string>();
  for (const task of [...tasks.data ?? [], ...history.data ?? []]) {
    if (task.conversation_id !== conversation || !task.call_key || !keys.includes(task.call_key) ||
        task.state !== 'complete' || task.outputs_expired ||
        (task.output_expires_at && Date.parse(task.output_expires_at) <= Date.now())) continue;
    for (const file of task.result?.files ?? []) {
      const session = file.storage_session_id ?? file.session_id ?? task.result?.session_id;
      if (session && /^[\w-]+$/.test(session) && /^[\w-]+$/.test(file.id) && /\.docx$/i.test(file.name)) {
        files.set(apiBaseUrl() + '/api/files/code/download/' + session + '/' + file.id, file.name);
      }
    }
  }
  const english = localize('com_ui_save') === 'Save';
  async function load(path: string) {
    const url = new URL(path, location.origin);
    if (!token || url.origin !== location.origin) throw Error('file_route');
    const response = await fetch(url, {headers: {Authorization: 'Bearer ' + token}, redirect: 'error'});
    if (!response.ok) throw Error('download');
    return response.blob();
  }
  async function download(path: string, name: string) {
    setError('');
    try {
      const url = URL.createObjectURL(await load(path));
      const link = document.createElement('a');
      link.href = url;
      link.download = name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch {
      setError(english ? 'Download failed. Try again.' : '下载失败，请重试。');
    }
  }
  return <>{Array.from(files, ([path, name]) => <div key={path} data-testid="office-word-delivery"
    className="border-border-light my-2 flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm">
    <strong className="break-all">{name}</strong>
    <button type="button" className="underline" onClick={() => void download(path, name)}>
      {english ? 'Download Word' : '下载 Word'}
    </button>
    <WordPages filename={name} load={() => load(path)} />
  </div>)}{error && <p role="alert">{error}</p>}</>;
}
