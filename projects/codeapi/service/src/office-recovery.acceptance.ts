/** Synthetic, isolated native Python acceptance; never run against a production endpoint. */
import { generateKeyPairSync, sign } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { durableExecutionId } from './durable-execution';
const root = '/work';
if (process.argv[2] === 'prepare') {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  writeFileSync(`${root}/acceptance-key.pem`, privateKey.export({ type: 'pkcs8', format: 'pem' }), { mode: 0o600 });
  writeFileSync(`${root}/.env`, `OFFICE_TEST_JWKS='${JSON.stringify({ keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'isolated-test', alg: 'EdDSA', use: 'sig' }] })}'\n`);
  process.exit(0);
}
const base = 'http://api:3112/v1/';
const privateKey = readFileSync(`${root}/acceptance-key.pem`);
function headers(owner = 'office-acceptance-alice') {
  const encode = (v: unknown) => Buffer.from(JSON.stringify(v)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  const text = encode({ alg: 'EdDSA', kid: 'isolated-test' }) + '.' + encode({ iss: 'librechat', aud: 'codeapi', sub: owner, iat: now, nbf: now - 1, jti: crypto.randomUUID(), principal_source: 'librechat_jwt', auth_context_hash: 'isolated-office-test', exp: now + 240, tenant_id: 'isolated-office-test' });
  return { Authorization: `Bearer ${text}.${sign(null, Buffer.from(text), privateKey).toString('base64url')}` };
}
async function request(path: string, init: RequestInit = {}) {
  const response = await fetch(base + path, { ...init, headers: { ...headers(), ...init.headers } });
  const text = await response.text();
  assert(response.ok, `${path}: ${response.status} ${text.slice(0, 1000)}`);
  return JSON.parse(text);
}
const mode = process.argv[2];
const taskKey = process.env.OFFICE_TEST_KEY ?? 'office-restart-file-task-0002';
if (mode === 'submit') {
  console.log('TEST_TASK_ID', durableExecutionId(JSON.stringify(['isolated-office-test', 'office-acceptance-alice']), taskKey));
  const refs = [];
  for (const [name, text] of [
    ['sales-a.csv', 'order,amount,cost\nA,120,60\nB,87.5,57\n'],
    ['sales-b.csv', 'order,amount,cost\nB,87.5,57\nC,100,60\n'],
  ]) {
    const body = new FormData(); body.set('kind', 'user'); body.set('id', 'office-acceptance-alice');
    body.set('file', new Blob([text], { type: 'text/csv' }), name);
    const result = await request('upload', { method: 'POST', body });
    refs.push(...result.files.map((file: any) => ({ id: file.fileId, name: file.filename, storage_session_id: result.storage_session_id, kind: 'user', resource_id: 'office-acceptance-alice' })));
  }
  writeFileSync(`${root}/input-refs.json`, JSON.stringify(refs));
  const code = `import csv, json, time, pathlib\nrows = {}\nfor name in ['sales-a.csv', 'sales-b.csv']:\n for r in csv.DictReader(open('/mnt/data/' + name)):\n  rows[r['order']] = r\nresult = {'revenue': sum(float(r['amount']) for r in rows.values()), 'cost': sum(float(r['cost']) for r in rows.values())}\nresult['profit'] = result['revenue'] - result['cost']\np = pathlib.Path('/mnt/data/execution-count.txt')\np.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\npathlib.Path('/mnt/data/merged.json').write_text(json.dumps(result))\ntime.sleep(8)\nprint(json.dumps(result))\n`;
  const payload = { lang: 'py', code, files: refs, timeout: 15000 };
  writeFileSync(`${root}/request.json`, JSON.stringify(payload));
  const run = request('exec', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Starbridge-Execution-Key': taskKey }, body: JSON.stringify(payload) });
  console.log('SUBMISSION_SENT');
  try { writeFileSync(`${root}/initial-result.json`, JSON.stringify(await run)); console.log('INITIAL_COMPLETE'); }
  catch (error) { console.log('SUBMISSION_INTERRUPTED', String(error)); }
} else if (mode === 'recover') {
  const key = taskKey;
  let status: any;
  for (let i = 0; i < 40; i++) {
    status = await request('tasks/' + key);
    if (status.state === 'complete') break;
    await Bun.sleep(500);
  }
  assert.equal(status.state, 'complete');
  assert(!status.result.stderr, JSON.stringify(status.result));
  assert.match(status.result.stdout, /307\.5/);
  assert.match(status.result.stdout, /130\.5/);
  const repeated = await request('exec', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Starbridge-Execution-Key': key }, body: readFileSync(`${root}/request.json`, 'utf8') });
  assert.deepEqual(repeated, status.result);
  const files: Record<string, string> = {};
  for (const file of status.result.files) {
    const response = await fetch(base + `download/${file.storage_session_id ?? status.result.session_id}/${file.id}?kind=user&id=office-acceptance-alice`, { headers: headers() });
    assert(response.ok, `download ${file.name}: ${response.status}`);
    files[file.name] = await response.text();
  }
  assert.equal(files['execution-count.txt'], '1');
  assert.equal(files['sales-a.csv'], 'order,amount,cost\nA,120,60\nB,87.5,57\n');
  assert.equal(files['sales-b.csv'], 'order,amount,cost\nB,87.5,57\nC,100,60\n');
  assert.deepEqual(JSON.parse(files['merged.json']), { revenue: 307.5, cost: 177, profit: 130.5 });
  const denied = await fetch(base + 'tasks/' + key, { headers: headers('office-acceptance-bob') });
  assert.equal(denied.status, 404);
  writeFileSync(`${root}/acceptance-result.json`, JSON.stringify({ state: status.state, session_id: status.result.session_id, file_count: status.result.files.length, files, repeated_result_identical: true, other_owner_status: denied.status }, null, 2));
  console.log('RECOVERY_FILES_AND_DEDUP_OK');
} else {
  console.log(JSON.stringify(await request('tasks/' + taskKey)));
}
