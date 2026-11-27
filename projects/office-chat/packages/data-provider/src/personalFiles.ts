import * as endpoints from './api-endpoints';

export type PersonalFileEntry = { name: string; kind: 'file' | 'directory'; size: number | null };
export type PersonalFileListing = { entries: PersonalFileEntry[] };
export type PersonalRevision = {
  version: number;
  path: string;
  size: number;
  sha256: string;
  savedAt: string;
  requestId: string;
  source?: { taskId: string; fileId: string };
};
export type PersonalHistory = { revisions: PersonalRevision[] };

/** Same immutable artifact and destination retain one save identity across refreshes. */
export async function personalSaveIdentity(
  owner: string,
  artifact: string,
  destination: string,
): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify([owner, artifact, destination]));
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 32);
  return `${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20)}`;
}

export class PersonalFileError extends Error {
  constructor(public readonly status: number) {
    super(`personal_files_${status}`);
  }
}

export function validPersonalFileName(value: string): boolean {
  return (
    !!value.trim() &&
    value !== '.' &&
    value !== '..' &&
    !/[\\/]/.test(value) &&
    ![...value].some((char) => char.charCodeAt(0) < 32)
  );
}

export async function personalFileRequest(
  token: string,
  operation: 'directories' | 'file' | 'usage' | 'revisions' | 'task-revision' | 'review-status',
  method: 'GET' | 'POST' | 'PUT' = 'GET',
  path = '',
  body?: Blob,
  revision?: { version?: number; requestId?: string; taskId?: string; fileId?: string; preview?: 'word' },
): Promise<Response> {
  const params = new URLSearchParams();
  if (revision?.preview) params.set('preview', revision.preview);
  if (revision?.version) params.set('version', String(revision.version));
  if (revision?.requestId) params.set('requestId', revision.requestId);
  if (revision?.taskId) params.set('taskId', revision.taskId);
  if (revision?.fileId) params.set('fileId', revision.fileId);
  const response = await fetch(
    endpoints.personalFiles(operation, path) + (params.size ? `&${params}` : ''),
    {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { 'Content-Type': 'application/octet-stream' } : {}),
      },
      credentials: 'same-origin',
      cache: 'no-store',
      body,
    },
  );
  if (!response.ok) throw new PersonalFileError(response.status);
  return response;
}
