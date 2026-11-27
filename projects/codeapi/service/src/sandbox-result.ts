import type * as t from './types';
import type { SandboxRawResponse } from './sandbox-backend';
import { summarizeSandboxResponse, summarizeText } from './execution-log';
import { applySystemReplacements, filterSystemLogs } from './utils';
import logger from './logger';

export function mapSandboxResult(
  responseData: SandboxRawResponse,
  isPyPlot: boolean | undefined,
  isSyntheticJob: boolean,
): t.ExecuteResult {
  if (!isSyntheticJob) {
    logger.info('Sandbox response', summarizeSandboxResponse(responseData));
  }

  const { files } = responseData;
  const run = responseData.run;
  const stdout = applySystemReplacements(run?.stdout ?? '');
  const stderr = filterSystemLogs(run?.stderr ?? '', isPyPlot);

  const result: t.ExecuteResult = {
    session_id: responseData.session_id,
    /* `files` is optional on the sandbox response (e.g. dry-run
     * execute with no outputs); the public `ExecuteResult.files` is
     * required and downstream callers always iterate it. Default to
     * `[]` so the strictened response type from Phase B doesn't
     * surface a regression that wasn't there before. */
    files: files ?? [],
    ...(responseData.deleted_files != null
      ? { deleted_files: responseData.deleted_files }
      : {}),
    ...(responseData.artifact_delivery != null
      ? { artifact_delivery: responseData.artifact_delivery }
      : {}),
    ...(responseData.artifact_truncation != null
      ? { artifact_truncation: responseData.artifact_truncation }
      : {}),
    stdout,
    stderr,
    ...(responseData.pending_tool_calls_payload != null
      ? {
          pending_tool_calls_payload:
            responseData.pending_tool_calls_payload,
        }
      : {}),
  };

  if (run) {
    result.code = run.code ?? null;
    result.signal = run.signal != null ? String(run.signal) : null;
    result.message = run.message ?? null;
    result.status = run.status ?? null;
    result.wall_time =
      ((run as Record<string, unknown>).wall_time as number | null) ?? null;
  }

  if (result.message || result.signal) {
    logger.warn('Sandbox execution error metadata', {
      session_id: responseData.session_id,
      code: result.code,
      signal: result.signal,
      message: summarizeText(result.message),
      status: result.status,
      wall_time: result.wall_time,
    });
  }

  return result;
}
