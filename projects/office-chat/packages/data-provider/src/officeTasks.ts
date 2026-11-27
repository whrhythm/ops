import { officeTasks } from './api-endpoints';

export type OfficeTask = {
  id: string;
  conversation_id: string;
  call_key?: string | null;
  state: 'accepted' | 'running' | 'complete' | 'unknown' | 'cancelling' | 'cancelled';
  output_expires_at?: string | null;
  outputs_expired?: boolean;
  inputs?: Array<{ id: string; storage_session_id: string; name: string; sha256?: string }>;
  created_at: string;
  updated_at: string;
  result?: {
    stdout?: string;
    stderr?: string;
    session_id?: string;
    files?: Array<{ id: string; name: string; session_id?: string; storage_session_id?: string }>;
  } | null;
};

export async function cancelOfficeTask(token: string, id: string): Promise<OfficeTask> {
  const response = await fetch(`${officeTasks('', id)}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error('office_tasks_unavailable');
  return response.json();
}

export async function fetchOfficeTasks(token: string, conversation: string): Promise<OfficeTask[]> {
  const get = async (id?: string) => {
    const response = await fetch(officeTasks(conversation, id), {
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error('office_tasks_unavailable');
    return response.json();
  };
  const listing: { tasks: OfficeTask[] } = await get();
  // Bound concurrent recovery reads; completed tasks are served from durable storage.
  const tasks = listing.tasks.slice(0, 50);
  for (let i = 0; i < tasks.length; i += 4) {
    await Promise.all(
      tasks.slice(i, i + 4).map(async (task, offset) => {
        if (task.state !== 'complete' && task.state !== 'cancelled')
          tasks[i + offset] = await get(task.id);
      }),
    );
  }
  return tasks;
}
