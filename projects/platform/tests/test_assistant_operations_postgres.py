"""Concurrent manual and model draft writes against disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from control_plane.ai_trials import Config, save_draft
from control_plane.assistant_operations import execute
from control_plane.models import (AIConfiguration, AIProjectBinding, AIProjectDraft,
    AuditEvent, CapacityAccount, ModelCredential, StorageMeasurement, UserEnvironment,
    WorkspaceFileService, WorkspaceProject, WorkspaceProvisioningIntent, WorkspaceVolumeBinding)
from control_plane.settings import get_settings
from control_plane.workspace_file_service import create_app
from test_ai_trials import initialized
from test_capacity import prepare_capacity
from test_postgres import postgres_harness


def test_manual_and_model_save_compete_for_same_revision(postgres_harness, monkeypatch, tmp_path):
    client, _ = postgres_harness
    _, auth, env_id, *_ = prepare_capacity(client, monkeypatch)
    monkeypatch.setenv('PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    get_settings.cache_clear()
    files = TestClient(create_app(str(tmp_path), 't' * 40))
    files.__enter__()

    def forward(db, principal, settings, method, endpoint, path, **kwargs):
        response = files.request(method, endpoint, params={'path': path, **kwargs.get('params', {})},
            headers={'Authorization': 'Bearer ' + 't' * 40}, content=kwargs.get('body'))
        if response.status_code >= 400:
            raise HTTPException(response.status_code, 'File operation could not be completed')
        return response

    try:
        _, _, path, config, initial = initialized((client, auth, env_id, tmp_path, forward), monkeypatch)
        project_id = path.split('/')[-1]
        barrier = Barrier(2)

        def attempt(model):
            with client.session_factory() as db:
                env = db.get(UserEnvironment, env_id)
                binding = db.get(AIProjectBinding, project_id)
                # Load stale objects before either competing transaction commits.
                db.get(AIProjectDraft, project_id)
                barrier.wait(timeout=15)
                try:
                    if model:
                        result, _, _ = execute(db, env_id, get_settings(), project_id,
                            initial['digest'], 'update_agent_draft', '{"instructions":"model edit"}')
                        return 200 if result.get('saved') else 409
                    save_draft(db, env, binding, get_settings(), initial['digest'],
                        Config.model_validate({**config, 'instructions': 'manual edit'}))
                    return 200
                except HTTPException as error:
                    return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(attempt, [False, True])) == [200, 409]
        current = client.get(path + '/trials', headers=auth).json()
        assert current['revision'] == initial['revision'] + 1
        assert current['config']['instructions'] in ('manual edit', 'model edit')
        with client.session_factory() as db:
            assert len(list(db.scalars(select(AuditEvent).where(
                AuditEvent.action == 'ai_trial.draft_updated')))) == 1
    finally:
        files.__exit__(None, None, None)
        # The harness has already rejected any nonlocal/non-test database.
        with client.session_factory.begin() as db:
            for model in (AIProjectDraft, AIProjectBinding, WorkspaceProject, ModelCredential,
                          AIConfiguration, StorageMeasurement, CapacityAccount, WorkspaceFileService,
                          WorkspaceVolumeBinding, WorkspaceProvisioningIntent):
                db.execute(delete(model))
            db.execute(delete(UserEnvironment).where(UserEnvironment.id == env_id))
