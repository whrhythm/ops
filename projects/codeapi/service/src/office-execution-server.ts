import axios from 'axios';
import express from 'express';
import { env } from './config';
import { DURABLE_EXECUTION_SECONDS, hasDurableExecution } from './durable-execution';
import { restoreGatewaySandboxResult, revokeGatewayEgressGrant } from './egress-gateway-client';
import { internalServiceAuthEnabled, requireConfiguredInternalServiceAuth } from './internal-service-auth';
import { commitJobResult, readCommittedJobResult, throwIfJobAborted } from './job-cancellation';
import logger from './logger';
import type { OfficeExecutionDispatchRequest } from './office-execution-dispatch';
import { connection, jobCancellationRegistry, queueNames } from './queue';
import type { SandboxRawResponse } from './sandbox-backend';
import { mapSandboxResult } from './sandbox-result';
import type { ExecuteResult } from './types';

if (!internalServiceAuthEnabled() || !env.HARDENED_SANDBOX_MODE || !env.EGRESS_GATEWAY_URL) {
  throw new Error('Office execution dispatch requires internal authentication and sandbox egress');
}

const app = express();
app.disable('x-powered-by');
let active = 0;
const runnerUrl = env.SANDBOX_ENDPOINT.replace(/\/+$/, '') + '/execute';

app.get('/health', (_req, res) => {
  res.sendStatus(connection.status === 'ready' ? 200 : 503);
});

app.post('/internal/office-execution', requireConfiguredInternalServiceAuth,
  express.json({ limit: env.HTTP_JSON_LIMIT }), async (req, res) => {
    const input = req.body as Partial<OfficeExecutionDispatchRequest> | undefined;
    const id = input?.jobId;
    const deadline = input?.deadlineAtMs;
    const payload = input?.payload;
    if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{21}$/.test(id) ||
        typeof deadline !== 'number' || !Number.isSafeInteger(deadline) ||
        deadline > Date.now() + env.JOB_TIMEOUT ||
        typeof input?.grantId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(input.grantId) ||
        payload?.language !== 'python' || typeof payload.session_id !== 'string' ||
        !/^sbx_[a-zA-Z0-9_-]{22}$/.test(payload.session_id) ||
        typeof payload.execution_manifest !== 'string' ||
        typeof payload.egress_grant !== 'string' || payload.tool_call_socket) {
      return res.status(400).json({ error: 'invalid_office_execution' });
    }
    const target = { jobId: id, queueName: queueNames.python };
    // Reserve capacity before awaiting I/O. A restarted worker can send another
    // task while its previous request still runs in this process.
    if (active >= env.PYTHON_CONCURRENCY) {
      return res.status(503).json({ error: 'office_execution_busy' });
    }
    active++;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort('deadline'), Math.max(0, deadline - Date.now()));
    let accepted = false;
    let registered = false;
    let completed = false;
    try {
      if (!await hasDurableExecution(connection, id)) {
        return res.status(404).json({ error: 'office_execution_not_found' });
      }
      const saved = await readCommittedJobResult<ExecuteResult>(connection, target);
      if (saved) return res.json(saved.result);
      // Keep a separate claim at the dispatch boundary. A dropped HTTP request
      // may have reached the runner, so never release this claim for a retry.
      accepted = (await connection.set(
        `codeapi:office-dispatch:v1:${id}`, '1', 'EX', DURABLE_EXECUTION_SECONDS, 'NX',
      )) === 'OK';
      if (!accepted) return res.status(409).json({ error: 'office_execution_already_dispatched' });

      await jobCancellationRegistry.register(target, controller);
      registered = true;
      throwIfJobAborted(controller.signal);
      if (Date.now() >= deadline) throw new Error('Office dispatch deadline exceeded');
      logger.info('Office execution dispatched', { jobId: id, sandboxSession: payload.session_id });
      // The worker's socket is deliberately excluded from this signal. This
      // process keeps the sandbox connection and commits the result before replying.
      const response = await axios.post<SandboxRawResponse>(runnerUrl, payload, {
        signal: controller.signal,
        maxRedirects: 0,
        maxContentLength: 16 * 1024 * 1024,
      });
      const restored = await restoreGatewaySandboxResult({
        grantId: input.grantId,
        egressGrantToken: payload.egress_grant,
        result: response.data,
        isSynthetic: input.isSynthetic === true,
        signal: controller.signal,
      });
      if (restored?.session_id !== id) throw new Error('Sandbox result identity mismatch');
      throwIfJobAborted(controller.signal);
      const result = mapSandboxResult(restored, input.isPyPlot === true, input.isSynthetic === true);
      const outcome = await commitJobResult(connection, target, result, DURABLE_EXECUTION_SECONDS, deadline);
      if (outcome !== 'committed') throw new Error('Office result commitment rejected');
      completed = true;
      return res.json(result);
    } catch {
      // Body, grants and upstream errors can contain private data.
      logger.warn('Office execution result could not be committed', { jobId: id });
      if (!res.destroyed) return res.status(503).json({ error: 'office_execution_result_unknown' });
    } finally {
      clearTimeout(timer);
      if (registered) await jobCancellationRegistry.unregister(target, controller);
      if (accepted) {
        await revokeGatewayEgressGrant({
          grantId: input.grantId,
          isSynthetic: input.isSynthetic === true,
          reason: completed ? 'completed' : 'failed',
        }).catch(() => logger.warn('Office execution grant revocation failed', { jobId: id }));
      }
      active--;
    }
  });

app.listen(Number(process.env.CODEAPI_OFFICE_EXECUTION_PORT) || 3114,
  process.env.CODEAPI_OFFICE_EXECUTION_BIND ?? '127.0.0.1');
