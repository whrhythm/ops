import { officeExecutionHeaders, withOfficeExecution } from './officeExecution';

it('keeps stable call keys across reconnects without leaking between concurrent calls', async () => {
  const [a, b] = await Promise.all([
    withOfficeExecution('conversation-a', 'call-1', async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      return officeExecutionHeaders();
    }),
    withOfficeExecution('conversation-b', 'call-1', async () => officeExecutionHeaders()),
  ]);
  expect(a['X-Starbridge-Execution-Key']).not.toBe(b['X-Starbridge-Execution-Key']);
  expect(
    await withOfficeExecution('conversation-a', 'call-1', async () => officeExecutionHeaders()),
  ).toEqual(a);
  expect(officeExecutionHeaders()).toEqual({});
});
