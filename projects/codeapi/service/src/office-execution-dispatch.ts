import axios from 'axios';
import { env } from './config';
import { internalServiceHeaders } from './internal-service-auth';
import type { ExecuteResult, PayloadBody } from './types';

export interface OfficeExecutionDispatchRequest {
  jobId: string;
  deadlineAtMs: number;
  payload: PayloadBody;
  grantId: string;
  isPyPlot: boolean;
  isSynthetic: boolean;
}

export async function dispatchOfficeExecution(
  request: OfficeExecutionDispatchRequest,
  signal: AbortSignal,
): Promise<ExecuteResult> {
  const response = await axios.post<ExecuteResult>(
    new URL('/internal/office-execution', env.OFFICE_EXECUTION_DISPATCH_URL).href,
    request,
    {
      headers: internalServiceHeaders(),
      signal,
      maxRedirects: 0,
      maxContentLength: 16 * 1024 * 1024,
    },
  );
  return response.data;
}
