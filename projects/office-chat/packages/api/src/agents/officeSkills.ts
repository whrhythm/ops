import { z } from 'zod';
import { Types } from 'mongoose';
import { Readable } from 'node:stream';
import { logger } from '@librechat/data-schemas';
import type { InitializeAgentDbMethods } from './initialize';
import type { ToolExecuteOptions } from './handlers';
import { parseSkillMarkdown } from '~/skills/parse';
import { isSafeSkillFilePath } from '~/skills/path';

const entrySchema = z.object({
  id: z.string().regex(/^skill-[a-f0-9]{26}$/),
  revision: z.number().int().positive(),
  digest: z.string().regex(/^[a-f0-9]{64}$/),
  package: z.object({
    name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,79}$/),
    description: z.string().max(2000),
    files: z.record(z.string(), z.string()),
    permissions: z.array(z.enum(['selected-files.read', 'python.execute'])),
  }),
});
type Entry = z.infer<typeof entrySchema>;
type Dependencies = {
  enabled: boolean;
  capabilities?: string[];
  timeoutMs: number;
  url?: string;
  serviceToken?: string;
  canExecuteCode: () => Promise<boolean>;
  user: { id: string; openidId?: string };
  fetch: (input: URL, init: RequestInit) => Promise<Response>;
  dbMethods: Pick<InitializeAgentDbMethods, 'getSkillByName' | 'listSkillsByAccess'>;
  toolDeps: Partial<ToolExecuteOptions>;
};

type OfficeSkillRuntime = {
  mergeIds: (ids?: Types.ObjectId[]) => Types.ObjectId[];
  dbMethods: Omit<Dependencies['dbMethods'], 'getSkillByName'> & {
    getSkillByName: NonNullable<ToolExecuteOptions['getSkillByName']>;
    listSkillsByAccess: NonNullable<InitializeAgentDbMethods['listSkillsByAccess']>;
  };
  toolDeps: Dependencies['toolDeps'] & {
    getSkillByName: NonNullable<ToolExecuteOptions['getSkillByName']>;
    getSkillFileByPath: NonNullable<ToolExecuteOptions['getSkillFileByPath']>;
    listSkillFiles: NonNullable<ToolExecuteOptions['listSkillFiles']>;
    getStrategyFunctions: NonNullable<ToolExecuteOptions['getStrategyFunctions']>;
  };
};

/** An installed package stays in the control plane; native tools read its approved snapshot per request. */
export async function createOfficeSkillRuntime(deps: Dependencies): Promise<OfficeSkillRuntime> {
  const { dbMethods: baseDb, toolDeps: baseTools } = deps;
  async function catalog(): Promise<Entry[]> {
    if (
      !deps.enabled ||
      !deps.capabilities?.includes('skills') ||
      !deps.user.openidId ||
      !deps.url ||
      !deps.serviceToken
    )
      return [];
    const target = new URL(deps.url.replace(/\/$/, '') + '/office-skills');
    if (
      target.protocol !== 'https:' ||
      target.username ||
      target.password ||
      target.search ||
      target.hash
    ) {
      throw new Error('office_skill_configuration_invalid');
    }
    const response = await deps.fetch(target, {
      headers: {
        'X-CodeChat-Service-Token': deps.serviceToken,
        'X-CodeChat-Subject': deps.user.openidId,
      },
      redirect: 'error',
      signal: AbortSignal.timeout(deps.timeoutMs),
    });
    if (response.status === 404) return [];
    if (!response.ok) throw new Error('office_skill_policy_unavailable');
    const text = await response.text();
    if (Buffer.byteLength(text) > 512 * 1024) throw new Error('office_skill_policy_too_large');
    return z.object({ skills: z.array(entrySchema).max(32) }).parse(JSON.parse(text)).skills;
  }
  let entries: Entry[] = [];
  try {
    entries = await catalog();
    if (entries.some((entry) => entry.package.permissions.includes('python.execute'))) {
      const codeAllowed = await deps.canExecuteCode();
      entries = entries.filter(
        (entry) => !entry.package.permissions.includes('python.execute') || codeAllowed,
      );
    }
  } catch {
    // Missing policy grants no skills. Existing ordinary chat can still proceed.
    logger.warn('[officeSkills] Approved catalog unavailable; no office skills granted');
  }
  const idFor = (entry: Entry) => new Types.ObjectId(entry.digest.slice(0, 24));
  const byId = new Map(entries.map((entry) => [idFor(entry).toString(), entry]));
  const byName = new Map(entries.map((entry) => [entry.package.name, entry]));
  async function current(entry: Entry) {
    const active = (await catalog()).find(
      (item) => item.id === entry.id && item.digest === entry.digest,
    );
    if (!active) throw new Error('office_skill_no_longer_available');
    return active;
  }
  function document(entry: Entry) {
    const parsed = parseSkillMarkdown(entry.package.files['SKILL.md']);
    if (parsed.name !== entry.package.name || parsed.parseError)
      throw new Error('office_skill_package_invalid');
    return {
      _id: idFor(entry),
      name: entry.package.name,
      description: entry.package.description,
      body: entry.package.files['SKILL.md'],
      frontmatter: parsed.frontmatter,
      author: new Types.ObjectId(deps.user.id),
      version: entry.revision,
      fileCount: Object.keys(entry.package.files).length - 1,
      deployment: true,
      source: 'inline' as const,
    };
  }
  const getSkillByName: NonNullable<ToolExecuteOptions['getSkillByName']> = async (
    name,
    ids,
    options,
  ) => {
    const entry = byName.get(name);
    if (!entry) return baseTools.getSkillByName?.(name, ids, options) ?? null;
    if (!ids.some((id) => id.toString() === idFor(entry).toString())) return null;
    return document(await current(entry));
  };
  const listSkillsByAccess: NonNullable<InitializeAgentDbMethods['listSkillsByAccess']> = async (
    params,
  ) => {
    const managed = params.accessibleIds.filter((id) => byId.has(id.toString()));
    const original = params.accessibleIds.filter((id) => !byId.has(id.toString()));
    const result = (await baseDb.listSkillsByAccess?.({ ...params, accessibleIds: original })) ?? {
      skills: [],
    };
    if (params.cursor) return result;
    const added = managed.map((id) => document(byId.get(id.toString())!));
    return { ...result, skills: [...added, ...result.skills] };
  };
  const getSkillFileByPath: NonNullable<ToolExecuteOptions['getSkillFileByPath']> = async (
    id,
    filename,
  ) => {
    const entry = byId.get(id.toString());
    if (!entry) return baseTools.getSkillFileByPath?.(id, filename) ?? null;
    if (!isSafeSkillFilePath(filename)) return null;
    await current(entry);
    const content = entry.package.files[filename];
    if (content === undefined) return null;
    return {
      content,
      isBinary: false,
      mimeType: 'text/markdown',
      bytes: Buffer.byteLength(content),
      filepath: `${id.toString()}/${filename}`,
      relativePath: filename,
      source: 'office_skill',
    };
  };
  const listSkillFiles: NonNullable<ToolExecuteOptions['listSkillFiles']> = async (id) => {
    const entry = byId.get(id.toString());
    if (!entry) return baseTools.listSkillFiles?.(id) ?? [];
    await current(entry);
    return Object.entries(entry.package.files)
      .filter(([name]) => name !== 'SKILL.md')
      .map(([name, content]) => ({
        relativePath: name,
        filename: name,
        filepath: `${id.toString()}/${name}`,
        source: 'office_skill',
        bytes: Buffer.byteLength(content),
      }));
  };
  const getStrategyFunctions: NonNullable<ToolExecuteOptions['getStrategyFunctions']> = (
    source,
  ) => {
    if (source !== 'office_skill') return baseTools.getStrategyFunctions?.(source) ?? {};
    return {
      getDownloadStream: async (_req, filepath) => {
        const separator = filepath.indexOf('/');
        const entry = byId.get(filepath.slice(0, separator));
        const filename = filepath.slice(separator + 1);
        if (!entry || !isSafeSkillFilePath(filename))
          throw new Error('office_skill_resource_unavailable');
        await current(entry);
        const content = entry.package.files[filename];
        if (content === undefined) throw new Error('office_skill_resource_unavailable');
        return Readable.from([Buffer.from(content)]);
      },
    };
  };
  const mergeIds = (ids: Types.ObjectId[] = []) => [
    ...ids.filter((id) => !byId.has(id.toString())),
    ...entries.map(idFor),
  ];
  return {
    mergeIds,
    dbMethods: { ...baseDb, getSkillByName, listSkillsByAccess },
    toolDeps: {
      ...baseTools,
      getSkillByName,
      getSkillFileByPath,
      listSkillFiles,
      getStrategyFunctions,
    },
  };
}
