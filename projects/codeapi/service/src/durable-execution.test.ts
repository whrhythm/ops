import { expect, test } from 'bun:test';
import { durableExecutionId, executionDigest, registerDurableExecution, hasDurableExecution } from './durable-execution';
import Redis from 'ioredis';

test('owner and request identity are isolated and JSON property order does not change the digest', () => {
  expect(durableExecutionId('alice', 'synthetic-call-0001')).not.toBe(durableExecutionId('bob', 'synthetic-call-0001'));
  expect(durableExecutionId('alice', 'synthetic-call-0001')).toMatch(/^[A-Za-z0-9_-]{21}$/);
  expect(executionDigest({ b: [1, 2], a: 'x' })).toBe(executionDigest({ a: 'x', b: [1, 2] }));
  expect(executionDigest({ a: 'x' })).not.toBe(executionDigest({ a: 'y' }));
  expect(() => durableExecutionId('alice', '../../escape')).toThrow();
});

test.skipIf(!process.env.OFFICE_TEST_REDIS)('concurrent registration has one immutable digest in real Redis', async () => {
  const redis = new Redis(process.env.OFFICE_TEST_REDIS!);
  const key = durableExecutionId('synthetic-test', `test-${crypto.randomUUID()}`);
  try {
    const attempts = await Promise.all(Array.from({ length: 12 }, () => registerDurableExecution(redis, key, 'a')));
    expect(attempts.every(Boolean)).toBe(true);
    expect(await registerDurableExecution(redis, key, 'b')).toBe(false);
    expect(await hasDurableExecution(redis, key)).toBe(true);
  } finally { await redis.quit(); }
});
