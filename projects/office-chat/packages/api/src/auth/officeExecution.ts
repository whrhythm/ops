import { createHash } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';

const current = new AsyncLocalStorage<{ key: string; conversation: string }>();

/** Carries only server-selected call identity, never model-provided headers. */
export function withOfficeExecution<T>(
  conversation: string,
  callId: string,
  execute: () => Promise<T>,
): Promise<T> {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(conversation) || !callId) return execute();
  const key = createHash('sha256')
    .update(JSON.stringify([conversation, callId]))
    .digest('hex');
  return current.run({ key, conversation }, execute);
}

export function officeExecutionHeaders(): Record<string, string> {
  const task = current.getStore();
  return task
    ? { 'X-Starbridge-Execution-Key': task.key, 'X-Starbridge-Conversation': task.conversation }
    : {};
}
