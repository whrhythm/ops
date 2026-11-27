import { createHash } from 'node:crypto';
import type IORedis from 'ioredis';

/** Retain receipts beyond queue cleanup. A missing receipt is never permission to replay. */
export const DURABLE_EXECUTION_SECONDS = 7 * 24 * 3600;
export const DURABLE_EXECUTION_HEADER = 'x-starbridge-execution-key';
export function durableExecutionId(owner: string, key: string): string {
  if (!/^[a-zA-Z0-9_-]{16,128}$/.test(key)) throw new Error('invalid_execution_key');
  // Native file/session routes require the 21-character NanoID wire format.
  // Keep 126 deterministic hash bits without widening any file authorization rule.
  return createHash('sha256').update(JSON.stringify([owner, key])).digest('base64url').slice(0, 21);
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map(k => `${JSON.stringify(k)}:${canonical(record[k])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}
export function executionDigest(body: unknown): string {
  return createHash('sha256').update(canonical(body)).digest('hex');
}
export async function registerDurableExecution(redis: IORedis, id: string, digest: string): Promise<boolean> {
  const key = `codeapi:durable-execution:v1:${id}`;
  // The receipt and digest are atomic; a conflicting request must not enqueue.
  return (await redis.eval(`
    local old = redis.call('GET', KEYS[1])
    if old then return old == ARGV[1] and 1 or 0 end
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return 1
  `, 1, key, digest, DURABLE_EXECUTION_SECONDS)) === 1;
}
export async function hasDurableExecution(redis: IORedis, id: string): Promise<boolean> {
  return (await redis.exists(`codeapi:durable-execution:v1:${id}`)) === 1;
}
