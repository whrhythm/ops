"""BFF-verified capabilities, separate from draft and authorization records."""
from datetime import timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .agent_tasks import seal, unseal
from .database import get_db
from .models import AIAuthoringCatalog, now
from .project_routing import resolve_project
from .settings import get_settings
from .workspace_gateway import own_environment


class NativeCatalog(BaseModel):
    model_config = ConfigDict(extra='forbid')
    native_owner_id: str = Field(pattern=r'^[0-9a-f]{24}$')
    actions: list['ActionCapability'] = Field(default_factory=list, max_length=24)
    skills: list['SkillCapability'] = Field(default_factory=list, max_length=24)
    calculator: bool = False
    mcp: bool = False


class SkillCapability(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reference: str = Field(pattern=r'^[0-9a-f]{24}$')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)


class ActionCapability(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reference: str = Field(pattern=r'^action:[A-Za-z0-9_-]{1,64}:[0-9a-f]{64}$')
    name: str = Field(min_length=1, max_length=64)
    service: str = Field(default='', max_length=80)
    description: str = Field(default='', max_length=500)
    data_notice: str = Field(default='', max_length=500)
    input_schema: dict = Field(default_factory=dict)


NativeCatalog.model_rebuild()


def read_catalog(db, project_id, settings):
    row = db.get(AIAuthoringCatalog, project_id)
    if not row or (now() - row.updated_at.replace(tzinfo=timezone.utc)).total_seconds() > 300:
        return {'actions': [], 'skills': [], 'calculator': False, 'mcp': False, 'stale': True}
    return {key: value for key, value in unseal(row.encrypted_content, settings).items()
            if key != 'project_plan'}


def create_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/ai-projects', tags=['authoring'])

    @router.post('/{project_id}/authoring-catalog')
    def sync(project_id: UUID, body: NativeCatalog, principal=Depends(principal_dependency), db=Depends(get_db)):
        settings = get_settings()
        env = own_environment(db, principal)
        db.refresh(env, with_for_update=True)
        kind, binding = resolve_project(db, env.id, str(project_id))
        if kind != 'assistant' or binding.native_owner_id != body.native_owner_id:
            raise HTTPException(404, 'ai_trials_not_found')
        data = body.model_dump(exclude={'native_owner_id'})
        import json
        if len(json.dumps(data)) > 64000:
            raise HTTPException(413, 'catalog_too_large')
        row = db.get(AIAuthoringCatalog, str(project_id))
        if not row:
            row = AIAuthoringCatalog(project_id=str(project_id))
            db.add(row)
        else:
            saved = unseal(row.encrypted_content, settings).get('project_plan')
            if saved is not None:
                data['project_plan'] = saved
        row.encrypted_content, row.updated_at = seal(data, settings), now()
        db.commit()
        return {'ok': True}

    return router
