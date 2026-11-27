import copy

from control_plane.models import AIConfiguration, AuditEvent, Tenant, UserEnvironment
from control_plane.settings import get_settings


def setup(harness, monkeypatch):
    client, token = harness
    monkeypatch.setenv('PLATFORM_OFFICE_SKILLS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_WORKSPACE_FILES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 's' * 40)
    get_settings.cache_clear()
    with client.session_factory() as db:
        tenant = Tenant(name='Skill test', owner_issuer=get_settings().oidc_issuer,
                        owner_subject='alice', project_limit=2)
        db.add(tenant)
        db.flush()
        environment = UserEnvironment(issuer=get_settings().oidc_issuer, subject='alice',
                                      identity_hash='a' * 64, storage_gib=1, first_tenant_id=tenant.id)
        db.add(environment)
        db.commit()
        environment_id = environment.id
    bundle = {
        'source': {'repository': 'anthropics/knowledge-work-plugins', 'commit': 'a' * 40,
                   'directory': 'data/skills/analyze'},
        'name': 'sb-data-analyze', 'description': 'Compare user-selected tables', 'version': '1.0.0',
        'files': {'SKILL.md': '---\nname: sb-data-analyze\n---\nRead [guide](references/guide.md).',
                  'references/guide.md': 'Use native Python and verify totals.', 'LICENSE.md': 'Apache 2.0'},
        'permissions': ['selected-files.read', 'python.execute'], 'environment_ids': [environment_id],
    }
    headers = {'Authorization': 'Bearer ' + token('reviewer', groups=['platform-admins'])}
    runtime = {'X-CodeChat-Service-Token': 's' * 40, 'X-CodeChat-Subject': 'alice'}
    return client, token, bundle, headers, runtime


def decision(client, headers, row, action):
    return client.post('/v1/admin/office-skills/' + row['id'] + '/decision', headers=headers,
                       json={'revision': row['revision'], 'digest': row['digest'],
                             'action': action, 'acknowledged': True})


def test_approval_install_scope_and_revocation(harness, monkeypatch):
    client, token, bundle, headers, runtime = setup(harness, monkeypatch)
    endpoint = '/v1/admin/office-skills'
    catalog = '/internal/v1/workspace/office-skills'
    assert client.post(endpoint, json=bundle, headers={'Authorization': 'Bearer ' + token()}).status_code == 403
    assert client.get(catalog, headers={**runtime, 'X-CodeChat-Service-Token': 'wrong'}).status_code == 403
    row = client.post(endpoint, json=bundle, headers=headers).json()
    assert client.post(endpoint, json=bundle, headers=headers).json()['id'] == row['id']
    assert client.get(catalog, headers=runtime).json() == {'skills': []}
    assert decision(client, headers, row, 'install').status_code == 409
    approved = decision(client, headers, row, 'approve').json()
    assert client.get(catalog, headers=runtime).json() == {'skills': []}
    assert decision(client, headers, row, 'approve').status_code == 409
    installed = decision(client, headers, approved, 'install').json()
    visible = client.get(catalog, headers=runtime).json()['skills']
    assert len(visible) == 1 and visible[0]['digest'] == installed['digest']
    assert client.get(catalog, headers={**runtime, 'X-CodeChat-Subject': 'bob'}).json() == {'skills': []}
    assert decision(client, headers, installed, 'revoke').json()['state'] == 'revoked'
    assert client.get(catalog, headers=runtime).json() == {'skills': []}
    with client.session_factory() as db:
        assert db.query(AuditEvent).filter(AuditEvent.action.like('office_skill.%')).count() == 4


def test_changed_digest_permissions_and_unresolved_resources_are_rejected(harness, monkeypatch):
    client, token, bundle, headers, runtime = setup(harness, monkeypatch)
    endpoint = '/v1/admin/office-skills'
    for files in ({**bundle['files'], '../escape.md': 'outside'},
                  {**bundle['files'], 'script.py': 'print(1)'},
                  {**bundle['files'], 'SKILL.md': '---\nname: sb-data-analyze\n---\n[missing](../CONNECTORS.md)'}):
        assert client.post(endpoint, json={**bundle, 'files': files}, headers=headers).status_code == 422
    assert client.post(endpoint, json={**bundle, 'permissions': ['network.access']}, headers=headers).status_code == 422
    assert client.post(endpoint, json={**bundle, 'environment_ids': ['00000000-0000-0000-0000-000000000000']}, headers=headers).status_code == 422
    row = client.post(endpoint, json=bundle, headers=headers).json()
    wrong = {**row, 'digest': '0' * 64}
    assert decision(client, headers, wrong, 'approve').status_code == 409


def test_tampered_persistent_package_fails_closed(harness, monkeypatch):
    client, token, bundle, headers, runtime = setup(harness, monkeypatch)
    row = client.post('/v1/admin/office-skills', json=bundle, headers=headers).json()
    approved = decision(client, headers, row, 'approve').json()
    installed = decision(client, headers, approved, 'install').json()
    with client.session_factory() as db:
        saved = db.get(AIConfiguration, installed['id'])
        config = copy.deepcopy(saved.config)
        config['package']['files']['SKILL.md'] += '\nUnreviewed change'
        saved.config = config
        db.commit()
    assert client.get('/internal/v1/workspace/office-skills', headers=runtime).status_code == 503


def test_second_installed_version_requires_explicit_retirement(harness, monkeypatch):
    client, token, bundle, headers, runtime = setup(harness, monkeypatch)
    row = client.post('/v1/admin/office-skills', json=bundle, headers=headers).json()
    approved = decision(client, headers, row, 'approve').json()
    installed = decision(client, headers, approved, 'install').json()
    second = client.post('/v1/admin/office-skills', json={**bundle, 'version': '1.0.1'}, headers=headers).json()
    second = decision(client, headers, second, 'approve').json()
    assert decision(client, headers, second, 'install').status_code == 409
    assert decision(client, headers, installed, 'revoke').status_code == 200
    assert decision(client, headers, second, 'install').status_code == 200
