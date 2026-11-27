import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from control_plane import project_tool_runtime as runtime


PROJECT = 'eee618e7-b31f-5f82-bb1b-315c8a23559b'
ENV = '11111111-1111-4111-8111-111111111111'
DIGEST = 'a' * 64
IMAGE = 'harbor.k3s.local/internal/project-tool@sha256:' + 'b' * 64


def ctx():
    return dict(project_id=PROJECT, environment_id=ENV, namespace='codechat-user-'+'c'*40,
                owner_hash='c'*40, pvc_uid='volume', source_digest=DIGEST, image=IMAGE, files={'app.py': 'd'*64},
                quotas=dict(agent_cpu_millicores=250, agent_memory_mib=128, agent_command_seconds=30))


def test_artifact_resolution_is_per_project_immutable_and_no_fallback():
    settings = SimpleNamespace(project_tool_images=json.dumps({PROJECT: {DIGEST: IMAGE}}))
    assert runtime.image_for(settings, PROJECT, DIGEST) == IMAGE
    for project, digest in [(ENV, DIGEST), (PROJECT, 'c'*64), (PROJECT, 'https://internal')]:
        with pytest.raises(ValueError):
            runtime.image_for(settings, project, digest)
    settings.project_tool_images = json.dumps({PROJECT: {DIGEST: 'image:latest'}})
    with pytest.raises(ValueError):
        runtime.image_for(settings, PROJECT, DIGEST)


def test_owner_context_rejects_cross_user_before_image_or_network():
    from control_plane.models import WorkspaceProject, UserEnvironment, WorkspaceVolumeBinding
    rows = {WorkspaceProject: SimpleNamespace(environment_id='another-owner'),
            UserEnvironment: SimpleNamespace(status='ready'), WorkspaceVolumeBinding: None}
    db = SimpleNamespace(get=lambda kind, key: rows[kind])
    with pytest.raises(ValueError, match='owner_mismatch'):
        runtime.context(db, None, {'project_id': PROJECT}, ENV)


def test_old_operator_urls_are_not_executable():
    from control_plane.project_connector import registered
    assert registered(SimpleNamespace(assistant_project_connectors=json.dumps({PROJECT: {
        'name': 'old', 'description': 'old', 'endpoint': 'http://legacy.platform-system.svc.cluster.local:8080/research'}})), PROJECT) is None


def test_workspace_permissions_have_no_system_workload_or_database_access():
    objects = runtime.workspace_objects(ctx()['namespace'], 'c'*64)
    assert {o['kind'] for o in objects} == {'RoleBinding', 'NetworkPolicy'}
    assert all(o['metadata']['namespace'] == ctx()['namespace'] for o in objects)
    assert 'postgres' not in json.dumps(objects)
    assert 'minio' not in json.dumps(objects)
    with pytest.raises(ValueError):
        runtime.workspace_objects('platform-system', 'c'*64)


def test_job_has_no_service_or_live_source_mutation_and_has_budget():
    job = runtime.manifest(ctx(), {'query': '武汉今天的天气', 'recent_days': 2})
    assert job['metadata']['namespace'] == ctx()['namespace']
    assert job['spec']['backoffLimit'] == 0
    assert job['spec']['ttlSecondsAfterFinished'] == 60
    assert job['spec']['activeDeadlineSeconds'] == 30
    pod = job['spec']['template']['spec']
    assert pod['runtimeClassName'] == 'kata-qemu-runtime-rs'
    assert pod['automountServiceAccountToken'] is False
    assert 'volumes' not in pod
    tool = pod['containers'][0]
    assert tool['resources']['limits']['cpu'] == '250m'
    assert tool['resources']['limits']['memory'] == '128Mi'
    assert tool['image'] == IMAGE
    assert tool['command'][:3] == ['python', '-B', '-c']
    assert not any('platform-system' in e['value'] for e in tool['env'])


def exercise(*, create=201, failed=False, pvc='volume', result=None, wrong_owner=False):
    seen = []
    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith('/persistentvolumeclaims/workspace-data'):
            return httpx.Response(200, json={'metadata': {'uid': pvc}})
        if req.method == 'POST':
            return httpx.Response(create, json={'metadata': {'uid': 'job-uid'}})
        if req.method == 'DELETE':
            return httpx.Response(200, json={})
        if req.url.path.endswith('/pods'):
            return httpx.Response(200, json={'items': [{'metadata': {'name': 'pod', 'ownerReferences': [
                {'kind': 'Job', 'uid': 'other' if wrong_owner else 'job-uid'}]}}]})
        if req.url.path.endswith('/log'):
            return httpx.Response(200, text=runtime.RESULT + json.dumps(result or {'sources': []}))
        return httpx.Response(200, json={'metadata': {'uid': 'job-uid'},
                                        'status': {'failed' if failed else 'succeeded': 1}})
    async def go():
        async with httpx.AsyncClient(base_url='https://cluster/', transport=httpx.MockTransport(handler)) as client:
            return await runtime.run(client, ctx(), {'query': '天气'})
    return go, seen


def test_result_and_uid_scoped_cleanup():
    go, seen = exercise()
    assert asyncio.run(go()) == {'sources': []}
    assert seen[-1][0] == 'DELETE'
    assert seen[-1][2]['preconditions'] == {'uid': 'job-uid'}


@pytest.mark.parametrize('kwargs,code', [({'failed': True}, 'execution_failed'),
    ({'wrong_owner': True}, 'ambiguous'), ({'create': 409}, 'busy'), ({'pvc': 'replaced'}, 'workspace_changed')])
def test_failure_does_not_replay_or_delete_other_invocations(kwargs, code):
    go, seen = exercise(**kwargs)
    with pytest.raises(ValueError, match=code):
        asyncio.run(go())
    assert sum(method == 'POST' for method, _, _ in seen) <= 1
    assert any(method == 'DELETE' for method, _, _ in seen) == ('failed' in kwargs or 'wrong_owner' in kwargs)


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('already_deleted', [False, True])
def test_confirmed_cleanup_waits_without_adopting_or_deleting_previous_job(monkeypatch, changed, already_deleted):
    posts, gets, deletes = [], [], []
    previous = {'metadata': {'uid': 'previous', 'deletionTimestamp': '2026-11-11T00:00:00Z',
                'labels': {'codechat.io/project-tool': 'true', 'codechat.io/user-hash': ctx()['owner_hash']}},
                'status': {'succeeded': 1}}
    async def no_wait(_):
        pass
    monkeypatch.setattr(runtime.asyncio, 'sleep', no_wait)
    def handler(req):
        if req.url.path.endswith('/persistentvolumeclaims/workspace-data'):
            return httpx.Response(200, json={'metadata': {'uid': 'volume'}})
        if req.method == 'POST':
            posts.append(req)
            return httpx.Response(409 if len(posts) == 1 or (already_deleted and changed) else 201,
                                  json={'metadata': {'uid': 'new'}})
        if req.method == 'DELETE':
            deletes.append(json.loads(req.content)['preconditions']['uid'])
            return httpx.Response(200, json={})
        if req.url.path.endswith('/pods'):
            return httpx.Response(200, json={'items': [{'metadata': {'name': 'pod',
                'ownerReferences': [{'kind': 'Job', 'uid': 'new'}]}}]})
        if req.url.path.endswith('/log'):
            return httpx.Response(200, text=runtime.RESULT + '{"sources": []}')
        gets.append(req)
        if len(gets) == 1:
            return httpx.Response(404) if already_deleted else httpx.Response(200, json=previous)
        if len(gets) == 2 and not already_deleted:
            return httpx.Response(200, json={'metadata': {'uid': 'concurrent'}}) if changed else httpx.Response(404)
        return httpx.Response(200, json={'metadata': {'uid': 'new'}, 'status': {'succeeded': 1}})
    async def go():
        async with httpx.AsyncClient(base_url='https://cluster/', transport=httpx.MockTransport(handler)) as client:
            return await runtime.run(client, ctx(), {'operation': 'search', 'query': 'example'})
    if changed:
        with pytest.raises(ValueError, match='busy'):
            asyncio.run(go())
        assert len(posts) == (2 if already_deleted else 1) and deletes == []
    else:
        assert asyncio.run(go()) == {'sources': []}
        assert len(posts) == 2 and deletes == ['new']
