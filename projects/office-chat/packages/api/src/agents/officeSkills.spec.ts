import { Types } from 'mongoose';
import type { ServerRequest } from '~/types';
import { createOfficeSkillRuntime } from './officeSkills';

const owner = new Types.ObjectId().toString();
const entry = {
  id: 'skill-' + '1'.repeat(26),
  revision: 3,
  digest: 'a'.repeat(64),
  package: {
    name: 'sb-data-analyze',
    permissions: ['selected-files.read', 'python.execute'],
    description: 'Compare uploaded tables',
    files: {
      'SKILL.md':
        '---\nname: sb-data-analyze\ndescription: Compare files\n---\nCheck [guide](references/guide.md).',
      'references/guide.md': 'Validate total and refund amounts.',
    },
  },
};

function dependencies() {
  let active = true;
  const fetch = jest.fn(
    async (_input: URL, _init: RequestInit) =>
      new Response(JSON.stringify({ skills: active ? [entry] : [] })),
  );
  return {
    enabled: true,
    capabilities: ['skills'],
    timeoutMs: 1000,
    url: 'https://platform.example/internal/v1/workspace',
    serviceToken: 's'.repeat(40),
    canExecuteCode: async () => true,
    user: { id: owner, openidId: 'selected-test-owner' },
    fetch,
    dbMethods: { listSkillsByAccess: jest.fn(async () => ({ skills: [] })) },
    toolDeps: {},
    revoke: () => {
      active = false;
    },
  };
}

describe('approved office skills in normal chat', () => {
  it('does not make a Python-dependent skill available when the existing role denies code execution', async () => {
    const deps = dependencies();
    const runtime = await createOfficeSkillRuntime({ ...deps, canExecuteCode: async () => false });
    expect(runtime.mergeIds([])).toEqual([]);
  });
  it('leaves disabled or capability-disabled chat without managed skills or network calls', async () => {
    for (const overrides of [{ enabled: false }, { capabilities: [] }]) {
      const deps = dependencies();
      const runtime = await createOfficeSkillRuntime({ ...deps, ...overrides });
      expect(runtime.mergeIds([])).toEqual([]);
      expect(deps.fetch).not.toHaveBeenCalled();
    }
  });

  it('uses server-owned subject, scopes native lookup and serves the complete reviewed files', async () => {
    const deps = dependencies();
    const runtime = await createOfficeSkillRuntime(deps);
    const ids = runtime.mergeIds([]);
    expect(ids).toHaveLength(1);
    expect(new Headers(deps.fetch.mock.calls[0][1].headers).get('X-CodeChat-Subject')).toBe(
      'selected-test-owner',
    );
    const catalog = await runtime.dbMethods.listSkillsByAccess({ accessibleIds: ids, limit: 100 });
    expect(catalog.skills.map((skill) => skill.name)).toEqual(['sb-data-analyze']);
    expect(await runtime.toolDeps.getSkillByName('sb-data-analyze', [])).toBeNull();
    expect((await runtime.toolDeps.getSkillByName('sb-data-analyze', ids))?.body).toContain(
      'Check [guide]',
    );
    expect(
      (await runtime.toolDeps.getSkillFileByPath(ids[0], 'references/guide.md'))?.content,
    ).toContain('refund');
    expect(await runtime.toolDeps.getSkillFileByPath(ids[0], '../guide.md')).toBeNull();
    expect(await runtime.toolDeps.getSkillFileByPath(ids[0], 'missing.md')).toBeNull();
    const files = await runtime.toolDeps.listSkillFiles(ids[0]);
    expect(files.map((file) => file.relativePath)).toEqual(['references/guide.md']);
    const stream = await runtime.toolDeps.getStrategyFunctions('office_skill').getDownloadStream!(
      {} as ServerRequest,
      ids[0].toString() + '/references/guide.md',
    );
    const chunks = [];
    for await (const chunk of stream)
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    expect(Buffer.concat(chunks).toString()).toContain('refund');
  });

  it('checks authoritative installation again when a tool tries to load after revocation', async () => {
    const deps = dependencies();
    const runtime = await createOfficeSkillRuntime(deps);
    const ids = runtime.mergeIds([]);
    deps.revoke();
    await expect(runtime.toolDeps.getSkillByName('sb-data-analyze', ids)).rejects.toThrow(
      'office_skill_no_longer_available',
    );
    await expect(
      runtime.toolDeps.getSkillFileByPath(ids[0], 'references/guide.md'),
    ).rejects.toThrow('office_skill_no_longer_available');
    expect((await createOfficeSkillRuntime(deps)).mergeIds([])).toEqual([]);
  });

  it('grants no managed capability when the policy response cannot be verified', async () => {
    const deps = dependencies();
    deps.fetch.mockResolvedValue(new Response('unavailable', { status: 503 }));
    expect((await createOfficeSkillRuntime(deps)).mergeIds([])).toEqual([]);
  });
});
