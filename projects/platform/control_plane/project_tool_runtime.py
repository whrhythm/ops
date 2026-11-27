"""Execute reviewed project artifacts as disposable compute in their owner's namespace.

No Service, shell, caller URL, writable workspace mount or platform namespace fallback.
The immutable image is a build output of the project files, not an editable live source.
"""
import asyncio
import json
from pathlib import Path
import re
import ssl
from uuid import UUID

import httpx

from .models import UserEnvironment, WorkspaceProject, WorkspaceVolumeBinding
from .plan_quotas import active_quotas


RESULT = 'PROJECT_TOOL_RESULT='
ENTRYPOINT = '''import asyncio,contextlib,hashlib,io,json,os
from pathlib import Path
import httpx
for name,expected in json.loads(os.environ["PROJECT_TOOL_FILES"]).items():
    if name not in ("app.py","requirements.txt") or hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:
        raise ValueError("source_mismatch")
from app import app
async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://artifact") as client:
        response=await client.get("/research",params=json.loads(os.environ["PROJECT_TOOL_INPUT"]),headers={"X-Expected-Artifact":os.environ["PROJECT_TOOL_ARTIFACT"]})
        response.raise_for_status()
        if response.headers.get("X-Project-Tool-Artifact")!=os.environ["PROJECT_TOOL_ARTIFACT"]:
            raise ValueError("artifact_mismatch")
        value=response.json()
        if not isinstance(value,dict) or len(response.content)>16000:
            raise ValueError("invalid_output")
        return value
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    value=asyncio.run(main())
print("PROJECT_TOOL_RESULT="+json.dumps(value,ensure_ascii=False))
'''


def workspace_objects(namespace, identity_hash):
    if not re.fullmatch('[a-f0-9]{64}', identity_hash) or namespace != 'codechat-user-' + identity_hash[:40]:
        raise ValueError('project_tool_namespace_invalid')
    meta = {'name': 'project-tools', 'namespace': namespace, 'labels': {
        'app.kubernetes.io/managed-by': 'platform-control', 'codechat.io/user-hash': identity_hash[:40]}}
    return [
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': meta,
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'codechat-project-tool-jobs'},
         'subjects': [{'kind': 'ServiceAccount', 'name': 'project-tool-client', 'namespace': 'platform-system'}]},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': meta,
         'spec': {'podSelector': {'matchLabels': {'codechat.io/project-tool': 'true'}},
            'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': [
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                         'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                 'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'search-system'}},
                         'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'search-gateway'}}}],
                 'ports': [{'protocol': 'TCP', 'port': 8080}]},
                {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': [
                    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                    '172.16.0.0/12', '192.0.0.0/24', '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4']}}],
                 'ports': [{'protocol': 'TCP', 'port': 443}]}]}}]


def image_for(settings, project_id, digest):
    project_id = str(UUID(project_id))
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('project_tool_invalid_artifact')
    try:
        image = json.loads(settings.project_tool_images)[project_id][digest]
        if not isinstance(image, str) or not re.fullmatch(r'harbor\.k3s\.local/[a-z0-9/_-]+@sha256:[a-f0-9]{64}', image):
            raise ValueError()
        return image
    except (KeyError, TypeError, ValueError):
        raise ValueError('project_tool_build_unavailable') from None


def context(db, settings, snapshot, environment_id):
    if not db or not environment_id:
        raise ValueError('project_tool_owner_required')
    project = db.get(WorkspaceProject, snapshot['project_id'])
    env = db.get(UserEnvironment, environment_id)
    binding = db.get(WorkspaceVolumeBinding, environment_id)
    if (not project or project.environment_id != environment_id or not env
            or env.status in ('deleted', 'suspended') or not binding
            or binding.namespace_name != 'codechat-user-' + env.identity_hash[:40]
            or not binding.pvc_uid):
        raise ValueError('project_tool_owner_mismatch')
    value = dict(namespace=binding.namespace_name, pvc_uid=binding.pvc_uid,
                 owner_hash=env.identity_hash[:40], environment_id=environment_id,
                 project_id=project.id, source_digest=snapshot['source_digest'],
                 files={name: snapshot['files'][name] for name in ('app.py', 'requirements.txt')},
                 image=image_for(settings, project.id, snapshot['source_digest']),
                 quotas=active_quotas(db, environment_id, settings))
    db.rollback()  # Never hold an identity / quota transaction during a network call.
    return value


def manifest(ctx, arguments):
    # One atomic K8s name per owner limits concurrency across API replicas/projects.
    name = 'project-tool-' + UUID(ctx['environment_id']).hex
    quota = ctx['quotas']
    cpu = min(500, quota['agent_cpu_millicores'])
    memory = min(256, quota['agent_memory_mib'])
    labels = {'codechat.io/project-tool': 'true', 'codechat.io/project-id': ctx['project_id'],
              'codechat.io/user-hash': ctx['owner_hash']}
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {
        'name': name, 'namespace': ctx['namespace'], 'labels': labels}, 'spec': {
        'backoffLimit': 0, 'activeDeadlineSeconds': min(60, quota['agent_command_seconds']),
        'ttlSecondsAfterFinished': 60, 'template': {'metadata': {'labels': labels}, 'spec': {
            'runtimeClassName': 'kata-qemu-runtime-rs', 'restartPolicy': 'Never',
            'terminationGracePeriodSeconds': 1,
            'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'tool', 'image': ctx['image'], 'workingDir': '/tool',
                'command': ['python', '-B', '-c', ENTRYPOINT],
                'env': [{'name': key, 'value': value} for key, value in {
                    'PROJECT_TOOL_ARTIFACT': ctx['source_digest'],
                    'PROJECT_TOOL_FILES': json.dumps(ctx['files']),
                    'PROJECT_TOOL_INPUT': json.dumps(arguments, ensure_ascii=False),
                    'SEARCH_PROVIDER_URL': 'http://search-gateway.search-system.svc.cluster.local:8080/search',
                    'PYTHONDONTWRITEBYTECODE': '1'}.items()],
                'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                              'limits': {'cpu': f'{cpu}m', 'memory': f'{memory}Mi', 'ephemeral-storage': '32Mi'}},
                'securityContext': {'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False,
                                    'capabilities': {'drop': ['ALL']}}}]}}}}


async def run(client, ctx, arguments):
    job = manifest(ctx, arguments)
    base = 'apis/batch/v1/namespaces/' + ctx['namespace'] + '/jobs'
    path = base + '/' + job['metadata']['name']
    pvc = await client.get('api/v1/namespaces/' + ctx['namespace'] + '/persistentvolumeclaims/workspace-data')
    if pvc.status_code != 200 or pvc.json()['metadata']['uid'] != ctx['pvc_uid']:
        raise ValueError('project_tool_workspace_changed')
    # A timeout at submission is not permission to resubmit or adopt an existing task.
    response = await client.post(base, json=job)
    if response.status_code == 409:
        # A confirmed conflict is different from an unknown submission. Only wait
        # for an already deleting, terminal task owned by this same environment.
        # Never adopt its result, delete it, or replay an uncertain POST.
        current = await client.get(path)
        if current.status_code == 404:
            # The conflicting Job can finish foreground deletion between POST
            # and GET. This is a confirmed rejection, followed by confirmed
            # absence; permit one new submission, never retry an unknown POST.
            response = await client.post(base, json=job)
            if response.status_code == 409:
                raise ValueError('project_tool_busy')
        else:
            previous = current.json() if current.status_code == 200 else {}
            meta, status = previous.get('metadata', {}), previous.get('status', {})
            labels = meta.get('labels', {})
            if not (meta.get('uid') and meta.get('deletionTimestamp')
                    and labels.get('codechat.io/project-tool') == 'true'
                    and labels.get('codechat.io/user-hash') == ctx['owner_hash']
                    and (status.get('succeeded') or status.get('failed'))):
                raise ValueError('project_tool_busy')
            for _ in range(30):
                await asyncio.sleep(0.5)
                current = await client.get(path)
                if current.status_code == 404:
                    response = await client.post(base, json=job)
                    break
                if current.status_code != 200 or current.json().get('metadata', {}).get('uid') != meta['uid']:
                    raise ValueError('project_tool_busy')
            if response.status_code == 409:
                raise ValueError('project_tool_busy')
    if response.status_code != 201:
        raise ValueError('project_tool_start_failed')
    uid = response.json()['metadata']['uid']
    try:
        async with asyncio.timeout(job['spec']['activeDeadlineSeconds'] + 15):
            while True:
                response = await client.get(path)
                if response.status_code != 200 or response.json()['metadata']['uid'] != uid:
                    raise ValueError('project_tool_task_changed')
                status = response.json().get('status', {})
                if status.get('failed'):
                    raise ValueError('project_tool_execution_failed')
                if status.get('succeeded'):
                    break
                await asyncio.sleep(0.5)
            response = await client.get('api/v1/namespaces/' + ctx['namespace'] + '/pods',
                                        params={'labelSelector': 'job-name=' + job['metadata']['name']})
            response.raise_for_status()
            pods = [p for p in response.json()['items'] if any(
                r.get('uid') == uid and r.get('kind') == 'Job' for r in p['metadata'].get('ownerReferences', []))]
            if len(pods) != 1:
                raise ValueError('project_tool_result_ambiguous')
            response = await client.get('api/v1/namespaces/' + ctx['namespace'] + '/pods/' +
                pods[0]['metadata']['name'] + '/log', params={'container': 'tool', 'limitBytes': 18000})
            response.raise_for_status()
            records = [line[len(RESULT):] for line in response.text.splitlines() if line.startswith(RESULT)]
            if len(records) != 1:
                raise ValueError('project_tool_result_missing')
            value = json.loads(records[0])
            if not isinstance(value, dict) or len(records[0].encode()) > 16000:
                raise ValueError('project_tool_result_invalid')
            return value
    finally:
        # UID prevents deleting another invocation. TTL also covers API process death.
        try:
            await client.request('DELETE', path, json={'apiVersion': 'v1', 'kind': 'DeleteOptions',
                'preconditions': {'uid': uid}, 'propagationPolicy': 'Foreground'})
        except (httpx.HTTPError, asyncio.CancelledError):
            pass


async def execute(db, settings, snapshot, arguments, environment_id):
    ctx = context(db, settings, snapshot, environment_id)
    sa = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    try:
        tls = ssl.create_default_context(cafile=str(sa / 'ca.crt'))
        async with httpx.AsyncClient(base_url='https://kubernetes.default.svc/', verify=tls,
                timeout=10, trust_env=False, headers={'Authorization': 'Bearer ' + (sa / 'token').read_text().strip()}) as client:
            return await run(client, ctx, arguments)
    except (httpx.HTTPError, OSError, TimeoutError) as error:
        raise ValueError('project_tool_runtime_unavailable') from error
