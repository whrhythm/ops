"""Construction adapter for reviewed, owner-scoped project tools."""
import json
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from fastapi import HTTPException

from .models import AIProjectBinding, UserEnvironment
from .project_routing import resolve_project
from .project_tool_registration import WebQuery


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=180)
    recent_days: int = Field(default=30, ge=1, le=30, strict=True)


def protocol(row):
    return row.descriptor['generated'].get('program', {}).get('protocol', 1) if row else 1


def arguments(row, raw):
    schema = WebQuery if protocol(row) == 2 else Input
    return schema.model_validate_json(raw).model_dump(exclude_none=True)


def registered(settings, project_id):
    # Legacy operator URL registrations bypassed project ownership and approval.
    # Only the reviewed project catalog may supply executable capabilities.
    return None


def approved(db, settings, project_id):
    if not settings.project_tool_registration_enabled:
        return None
    from .ai_trials import draft_snapshot
    from .generated_tools import generated_row
    binding = db.get(AIProjectBinding, project_id)
    if not binding or binding.state != 'ready':
        return None
    try:
        _, config, _ = draft_snapshot(db, binding, settings)
        if not (config.tool_reference or '').startswith('projecttool:'):
            return None
        row = generated_row(db, project_id, config.tool_reference)
        return row if row.descriptor['generated']['engine'] == 'project-http-v1' else None
    except HTTPException:
        return None


async def invoke(db, settings, env_id, project_id, raw):
    def owned():
        kind, _ = resolve_project(db, env_id, project_id)
        env = db.get(UserEnvironment, env_id, populate_existing=True)
        return kind == 'assistant' and env and env.status not in ('deleted', 'suspended')

    if not settings.assistant_planning_enabled or not owned():
        return {'ok': False, 'code': 'project_connector_unavailable'}
    reviewed = approved(db, settings, project_id)
    if not reviewed:
        return {'ok': False, 'code': 'project_connector_unavailable'}
    data = arguments(reviewed, raw)
    if reviewed:
        from .project_tool_registration import execute
        snapshot, reference = reviewed.descriptor['generated'], reviewed.reference
        db.rollback()
        try:
            value = json.loads(await execute(settings, snapshot, data, db=db, environment_id=env_id))
        except ValueError:
            return {'ok': False, 'code': 'project_connector_failed'}
        fresh = approved(db, settings, project_id) if owned() else None
        return ({'ok': True, 'external_content_is_untrusted': True, 'result': value}
                if fresh and fresh.reference == reference else {'ok': False, 'code': 'project_connector_unavailable'})


def source_links(value):
    if not isinstance(value, dict) or not isinstance(value.get('sources'), list):
        return []
    links = []
    for item in value['sources'][:3]:
        if not isinstance(item, dict) or not item.get('excerpt') or not isinstance(item.get('url'), str):
            continue
        url = item['url']
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
            links.append(quote(url, safe=":/?&=%;,+@!$'~*-._"))
    return links
