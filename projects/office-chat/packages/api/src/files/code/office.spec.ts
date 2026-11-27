import {
  codeFileReadTimeout,
  createOfficeCodeCallQueue,
  normalizeAdmittedCodeToolName,
  officeExecutionGuidance,
  rejectsNonPythonCode,
} from './office';

it('rejects shell and malformed Python calls only in native Python mode', () => {
  for (const name of ['bash', 'bash_tool']) expect(rejectsNonPythonCode(name, {}, true)).toBe(true);
  for (const args of [null, {}, { lang: 'bash' }, { lang: 'js' }, 'print(1)']) {
    expect(rejectsNonPythonCode('execute_code', args, true)).toBe(true);
  }
  expect(rejectsNonPythonCode('execute_code', { lang: 'py', code: 'print(1)' }, true)).toBe(false);
  expect(rejectsNonPythonCode('calculator', {}, true)).toBe(false);
  expect(rejectsNonPythonCode('execute_code', { lang: 'bash' }, false)).toBe(false);
});

it('preserves legacy timeout by default and bounds deployment configuration', () => {
  expect(codeFileReadTimeout()).toBe(15000);
  expect(codeFileReadTimeout(60000)).toBe(60000);
  for (const n of [0, -1, 1.5, NaN, Infinity, 120001]) expect(codeFileReadTimeout(n)).toBe(15000);
});

it('uses admitted tool names, requires readback, and never offers tools when absent', () => {
  expect(officeExecutionGuidance([])).toBe('');
  const text = officeExecutionGuidance(['bash_tool', 'read_file']);
  expect(text).toContain('bash_tool, read_file');
  expect(text).toContain('reopen the output workbook');
  expect(text).toContain('exact values');
  expect(text).toContain('before repeating a write');
  expect(text).not.toContain('execute_code');
  expect(text).toContain('bash is the shell language, not a callable function name');
  expect(text).toContain('sequentially');
  expect(officeExecutionGuidance(['read_file'])).not.toContain('bash_tool(');
});

it('normalizes only the observed bash spelling when bash_tool is admitted', () => {
  expect(normalizeAdmittedCodeToolName('bash', ['bash_tool', 'read_file'])).toBe('bash_tool');
  expect(normalizeAdmittedCodeToolName('bash', ['read_file'])).toBe('bash');
  expect(normalizeAdmittedCodeToolName('bash', undefined)).toBe('bash');
  expect(normalizeAdmittedCodeToolName('python', ['bash_tool', 'read_file'])).toBe('python');
});

it('queues same-key office commands in order and releases the next call after a failure', async () => {
  const queue = createOfficeCodeCallQueue();
  const order: string[] = [];
  let releaseFirst!: () => void;
  const gate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const first = queue('bash_tool', async () => {
    order.push('first:start');
    await gate;
    order.push('first:end');
    throw new Error('first failed');
  });
  const second = queue('bash_tool', async () => {
    order.push('second:start');
    return 'second result';
  });
  const independent = queue('read_file', async () => {
    order.push('read:start');
    return 'read result';
  });

  await expect(independent).resolves.toBe('read result');
  expect(order).toEqual(['first:start', 'read:start']);
  releaseFirst();
  await expect(first).rejects.toThrow('first failed');
  await expect(second).resolves.toBe('second result');
  expect(order).toEqual(['first:start', 'read:start', 'first:end', 'second:start']);
});
