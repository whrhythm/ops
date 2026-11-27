from concurrent.futures import ThreadPoolExecutor
from base64 import b64encode
from datetime import timedelta
from pathlib import Path
import os
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from control_plane import main
from control_plane.auth import Principal, current_principal
from control_plane.database import get_db, make_engine
from control_plane.enterprises import IssueInput, issue, redeem, submit
from control_plane.models import AccountAccess, AuditEvent, Base, Enterprise, EnterpriseApplication, EnterpriseEntitlement, EnterpriseMembership, now
from control_plane.settings import get_settings

ISSUER = 'https://identity.example'
ISSUE = {'revision': 1, 'code_days': 2, 'trial_days': 14, 'limits': {'seats': 3, 'storage_gib': 5, 'compute_minutes': 60}}
USER_ROUTE = '/internal/v1/workspace/enterprise'
ADMIN_ROUTE = '/v1/admin/enterprises/applications'


@pytest.fixture
def enterprise_api(tmp_path, monkeypatch):
    engine = make_engine(f'sqlite:///{tmp_path / "enterprise.db"}')
    monkeypatch.setenv('PLATFORM_DATABASE_URL', str(engine.url))
    monkeypatch.setenv('PLATFORM_ENTERPRISE_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_WORKSPACE_FILES_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_LIBRECHAT_SERVICE_TOKEN', 't' * 48)
    monkeypatch.setenv('PLATFORM_OIDC_ISSUER', ISSUER)
    get_settings.cache_clear()
    monkeypatch.setattr(main, 'engine', engine)
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    command.upgrade(config, 'head')

    def database():
        with Session(engine, expire_on_commit=False) as db:
            yield db

    main.app.dependency_overrides[get_db] = database
    main.app.dependency_overrides[current_principal] = lambda: Principal(ISSUER, 'member')
    try:
        with TestClient(main.app) as client:
            yield client, engine
    finally:
        main.app.dependency_overrides.pop(get_db)
        main.app.dependency_overrides.pop(current_principal)
        get_settings.cache_clear()
        engine.dispose()


def headers(subject):
    return {'X-CodeChat-Service-Token': 't' * 48, 'X-CodeChat-Subject': subject}


def grant_admin():
    main.app.dependency_overrides[current_principal] = lambda: Principal(
        ISSUER, 'operator', (get_settings().admin_group,),
    )


def test_enterprise_membership_revocation_preserves_versions(enterprise_api, monkeypatch):
    client, engine = enterprise_api
    monkeypatch.setenv('PLATFORM_ENTERPRISE_COLLABORATION_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr('control_plane.enterprise_collaboration.IdentityClient.call',
                        lambda *args, **kw: {'results': [{'username': kw['params']['username'],
                            'uid': kw['params']['username'], 'is_active': True}]})
    grant_admin()
    code = sign(client, create_application(client))
    enterprise = client.post(f'{USER_ROUTE}/activate', headers=headers('alice'), json={'code': code}).json()
    path = f'{USER_ROUTE}/{enterprise["id"]}'

    def send(method, path, subject, data=None):
        return client.request(method, path, headers=headers(subject), json=data)

    for name, role in [('bob', 'viewer'), ('carol', 'editor')]:
        invite = send('POST', path + '/invite', 'alice', {'username': name, 'role': role})
        assert invite.status_code == 200, invite.text
        invitation_id = invite.json()['id']
        assert send('GET', path + '/files', name).status_code == 404
        accept = f'{USER_ROUTE}/invitations/{invitation_id}/accept'
        assert send('POST', accept, 'outsider').status_code == 404
        assert send('GET', USER_ROUTE + '/invitations', name).json()[0]['id'] == invitation_id
        assert send('POST', accept, name).json()['role'] == role
        assert send('POST', accept, name).json()['role'] == role
    assert send('POST', path + '/invite', 'carol', {'username': 'outsider', 'role': 'editor'}).status_code == 403
    assert send('POST', path + '/invite', 'alice', {'username': 'outsider', 'role': 'viewer'}).status_code == 409
    data = lambda raw: {'request_id': str(uuid4()), 'content': b64encode(raw).decode()}
    create = {'name': 'budget.csv', **data(b'original')}
    assert send('POST', path + '/files', 'bob', create).status_code == 403
    result = send('POST', path + '/files', 'alice', create)
    assert result.status_code == 200, result.text
    asset = result.json()['id']
    assert send('POST', path + '/files', 'alice', create).json()['id'] == asset
    versions = f'{path}/files/{asset}/versions'
    assert send('GET', versions + '/1', 'bob').content == b'original'
    update = {'base': 1, **data(b'revised')}
    assert send('POST', versions, 'bob', update).status_code == 403
    assert send('POST', versions, 'carol', update).json()['head'] == 2
    assert send('POST', versions, 'carol', update).json()['head'] == 2
    assert send('POST', versions, 'carol', {'base': 1, **data(b'stale')}).status_code == 409
    invites = send('GET', path + '/members', 'alice').json()
    for invite in invites:
        revoke = f'{path}/invitations/{invite["id"]}/revoke'
        assert send('POST', revoke, 'alice').status_code == 200
        assert send('GET', versions, invite['username']).status_code == 404
        assert send('GET', versions + '/1', invite['username']).status_code == 404
        assert send('POST', versions, invite['username'], update).status_code == 404
        assert send('POST', f'{USER_ROUTE}/invitations/{invite["id"]}/accept', invite['username']).status_code == 404
    assert send('GET', versions + '/1', 'alice').content == b'original'
    assert send('GET', versions + '/2', 'alice').content == b'revised'
    assert len(send('GET', versions, 'alice').json()) == 2
    assert send('GET', versions, 'outsider').status_code == 404
    waiting = send('POST', path + '/invite', 'alice', {'username': 'outsider', 'role': 'viewer'}).json()['id']
    assert send('POST', f'{path}/invitations/{waiting}/revoke', 'alice').status_code == 200
    assert send('POST', f'{USER_ROUTE}/invitations/{waiting}/accept', 'outsider').status_code == 404


def create_application(client, name='星桥试点'):
    response = client.post(f'{USER_ROUTE}/apply', headers=headers('alice'), json={'name': name})
    assert response.status_code == 200
    return response.json()


def sign(client, application, **changes):
    response = client.post(f'{ADMIN_ROUTE}/{application["id"]}/issue', json={**ISSUE, **changes})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    return response.json()['code']


def test_enterprise_opening_and_retry(enterprise_api):
    client, engine = enterprise_api
    assert client.get(USER_ROUTE).status_code == 403
    application = create_application(client)
    assert create_application(client, ' 星桥试点 ')['id'] == application['id']
    assert client.get(ADMIN_ROUTE).status_code == 403
    grant_admin()
    code = sign(client, application)
    assert code not in client.get(ADMIN_ROUTE).text
    for subject, status in [('bob', 404), ('alice', 200), ('alice', 200)]:
        response = client.post(f'{USER_ROUTE}/activate', headers=headers(subject), json={'code': code})
        assert response.status_code == status
    enterprise = response.json()
    assert enterprise['role'] == 'owner' and enterprise['limits'] == ISSUE['limits']
    assert client.get(USER_ROUTE, headers=headers('bob')).json() == {'applications': [], 'enterprises': []}
    setup = f'{USER_ROUTE}/{enterprise["id"]}/setup'
    assert client.post(setup, headers=headers('bob'), json={'step': 1}).status_code == 404
    assert client.post(setup, headers=headers('alice'), json={'step': 2}).status_code == 409
    for step in (1, 1, 2, 3, 4, 5):
        assert client.post(setup, headers=headers('alice'), json={'step': step}).json()['setup_step'] == step
    assert client.get(USER_ROUTE, headers=headers('alice')).json()['enterprises'][0]['setup_step'] == 5
    with Session(engine) as db:
        for model in (Enterprise, EnterpriseMembership, EnterpriseEntitlement):
            assert db.scalar(select(func.count()).select_from(model)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'enterprise.activated')) == 1
        assert db.get(EnterpriseApplication, application['id']).code_digest != code


def test_expired_revoked_and_delegated_codes(enterprise_api, monkeypatch):
    client, engine = enterprise_api
    application = create_application(client)
    grant_admin()
    original = sign(client, application)
    redeem_path = f'{USER_ROUTE}/activate'
    with Session(engine) as db:
        db.get(EnterpriseApplication, application['id']).code_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post(redeem_path, headers=headers('alice'), json={'code': original}).status_code == 404
    revoke_path = f'{ADMIN_ROUTE}/{application["id"]}/revoke'
    assert client.post(revoke_path, json={'revision': 2}).status_code == 204
    monkeypatch.setattr('control_plane.enterprises.IdentityClient.call',
                        lambda *args: {'uid': 'bob', 'is_active': True})
    delegated = sign(client, application, owner_account_id=42, revision=3)
    assert client.post(revoke_path, json={'revision': 2}).status_code == 409
    assert client.post(redeem_path, headers=headers('alice'), json={'code': delegated}).status_code == 404
    assert client.post(redeem_path, headers=headers('bob'), json={'code': original}).status_code == 404
    invalid = client.post(redeem_path, headers=headers('bob'), json={'code': 'do-not-echo'})
    assert invalid.status_code == 422 and 'do-not-echo' not in invalid.text
    response = client.post(redeem_path, headers=headers('bob'), json={'code': delegated})
    assert response.status_code == 200
    enterprise = response.json()
    with Session(engine) as db:
        db.get(EnterpriseEntitlement, enterprise['id']).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post(f'{USER_ROUTE}/{enterprise["id"]}/setup', headers=headers('bob'), json={'step': 1}).status_code == 403
    assert client.get(USER_ROUTE, headers=headers('bob')).json()['enterprises'][0]['status'] == 'expired'
    with Session(engine) as db:
        db.add(AccountAccess(issuer=ISSUER, subject='bob', blocked=True))
        db.commit()
    assert client.get(USER_ROUTE, headers=headers('bob')).status_code == 403


def test_postgres_concurrent_redemption():
    url = os.getenv('PLATFORM_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires disposable PostgreSQL')
    parsed = make_url(url)
    assert parsed.host in {'127.0.0.1', 'localhost'} and parsed.database.startswith('platform_test_')
    engine = make_engine(url)
    tables = [model.__table__ for model in (AuditEvent, AccountAccess, EnterpriseApplication,
              Enterprise, EnterpriseMembership, EnterpriseEntitlement)]
    Base.metadata.create_all(engine, tables=tables)
    principal = Principal(ISSUER, 'concurrent-owner')
    with Session(engine, expire_on_commit=False) as db:
        application = submit(db, principal, 'Concurrent redemption')
        code = issue(db, Principal(ISSUER, 'reviewer'), application.id, IssueInput(**ISSUE)).code
    barrier = Barrier(2, timeout=10)

    def activate():
        with Session(engine, expire_on_commit=False) as db:
            barrier.wait()
            return redeem(db, principal, code).id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: activate(), range(2)))
        assert len(set(ids)) == 1
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(Enterprise).where(
                Enterprise.application_id == application.id)) == 1
    finally:
        engine.dispose()


def test_enterprise_schema_rollout(tmp_path, monkeypatch):
    url = f'sqlite:///{tmp_path / "enterprise-rollout.db"}'
    monkeypatch.setenv('PLATFORM_DATABASE_URL', url)
    monkeypatch.setenv('PLATFORM_ENTERPRISE_ENABLED', 'false')
    get_settings.cache_clear()
    engine = make_engine(url)
    monkeypatch.setattr(main, 'engine', engine)
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    try:
        command.upgrade(config, '0034_authoring_catalog')
        with TestClient(main.app) as client:
            assert client.get('/healthz').status_code == 200
            assert client.get(USER_ROUTE).status_code == 404
        monkeypatch.setenv('PLATFORM_ENTERPRISE_ENABLED', 'true')
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match='Database schema'):
            with TestClient(main.app):
                pass
        command.upgrade(config, 'head')
        with Session(engine) as db:
            submit(db, Principal(ISSUER, 'rollout-owner'), 'Preserve on rollback')
        with pytest.raises(RuntimeError, match='Preserve enterprise records'):
            command.downgrade(config, '0034_authoring_catalog')
        monkeypatch.setenv('PLATFORM_ENTERPRISE_ENABLED', 'false')
        get_settings.cache_clear()
        with TestClient(main.app) as client:
            assert client.get('/healthz').status_code == 200
        with engine.connect() as conn:
            assert conn.scalar(text('SELECT COUNT(*) FROM enterprise_applications')) == 1
    finally:
        engine.dispose()
        get_settings.cache_clear()
