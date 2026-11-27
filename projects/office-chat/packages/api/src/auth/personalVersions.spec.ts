import { personalVersions, VersionError, versionRoot } from './personalVersions';

function setup() {
  const files = new Map<string, Buffer>();
  const dirs = new Set<string>();
  let loseReply = false;
  const fetch = jest.fn(async (url: URL, init: RequestInit) => {
    const headers = new Headers(init.headers);
    const owner = headers.get('X-CodeChat-Subject');
    const path = url.searchParams.get('path') ?? '';
    const key = `${owner}:${path}`;
    const op = url.pathname.split('/').pop();
    if (op === 'directories') {
      if (init.method === 'POST') {
        if (dirs.has(key)) return new Response('{}', { status: 409 });
        dirs.add(key);
        return new Response('{}', { status: 201 });
      }
      if (!dirs.has(key)) return new Response('{}', { status: 404 });
      return Response.json({
        entries: [...files.keys()]
          .filter((k) => k.startsWith(key + '/'))
          .map((k) => ({ name: k.slice(key.length + 1), kind: 'file' })),
      });
    }
    if (init.method === 'PUT') {
      if (files.has(key)) return new Response('{}', { status: 409 });
      files.set(key, Buffer.from(init.body as Uint8Array));
      if (loseReply && path.endsWith('/2.snapshot')) {
        loseReply = false;
        throw new Error('connection lost');
      }
      return new Response('{}', { status: 201 });
    }
    return files.has(key)
      ? new Response(new Uint8Array(files.get(key)!))
      : new Response('{}', { status: 404 });
  });
  const deps = { url: 'https://storage.test/v1', serviceToken: 'private', fetch };
  const api = (owner = 'a') => personalVersions(deps, owner, 'report.xlsx');
  files.set('a:report.xlsx', Buffer.from('original'));
  return {
    files,
    fetch,
    api,
    lose: () => {
      loseReply = true;
    },
  };
}
const id = '11111111-1111-4111-8111-111111111111';
const second = '22222222-2222-4222-8222-222222222222';

it('freezes the original and saves downloadable revisions without overwriting the source', async () => {
  const c = setup();
  await c.api().save(2, id, Buffer.from('revision2'));
  await c.api().save(3, second, Buffer.from('revision3'));
  expect(c.files.get('a:report.xlsx')?.toString()).toBe('original');
  c.files.set('a:report.xlsx', Buffer.from('external later change'));
  expect((await c.api().download(1)).toString()).toBe('original');
  expect((await c.api().download(2)).toString()).toBe('revision2');
  expect((await c.api().list()).map((v) => v.version)).toEqual([3, 2, 1]);
});
it('recovers a lost write response with the same request without creating another version', async () => {
  const c = setup();
  c.lose();
  await expect(c.api().save(2, id, Buffer.from('revision2'))).rejects.toThrow('connection lost');
  await c.api().save(2, id, Buffer.from('revision2'));
  expect(await c.api().list()).toHaveLength(2);
  await expect(c.api().save(2, id, Buffer.from('different'))).rejects.toMatchObject({
    status: 409,
  });
});
it('admits only one concurrent writer for the same version and does not silently renumber', async () => {
  const c = setup();
  const outcomes = await Promise.allSettled([
    c.api().save(2, id, Buffer.from('one')),
    c.api().save(2, second, Buffer.from('two')),
  ]);
  expect(outcomes.filter((o) => o.status === 'fulfilled')).toHaveLength(1);
  expect((await c.api().list()).map((v) => v.version)).toEqual([2, 1]);
});
it('isolates histories and downloads by authenticated subject', async () => {
  const c = setup();
  await c.api().save(2, id, Buffer.from('secret'));
  expect(await c.api('b').list()).toEqual([]);
  await expect(c.api('b').download(2)).rejects.toMatchObject({ status: 404 });
});
it('does not advertise success when storage fails, or an invalid existing envelope is read', async () => {
  const c = setup();
  c.fetch.mockImplementationOnce(async () => new Response('private', { status: 507 }));
  await expect(c.api().save(2, id, Buffer.from('new'))).rejects.toEqual(new VersionError(507));
  expect(c.files.size).toBe(1);
  await c.api().save(2, id, Buffer.from('new'));
  const path = [...c.files.keys()].find(
    (k) => k.includes(versionRoot) && k.endsWith('2.snapshot'),
  )!;
  c.files.set(path, Buffer.from('corrupt'));
  await expect(c.api().download(2)).rejects.toMatchObject({ status: 503 });
});
it('rejects revision gaps and refuses new versions without an original', async () => {
  const c = setup();
  await expect(c.api().save(4, id, Buffer.from('new'))).rejects.toMatchObject({ status: 409 });
  await expect(c.api('b').save(2, id, Buffer.from('new'))).rejects.toMatchObject({ status: 404 });
});
