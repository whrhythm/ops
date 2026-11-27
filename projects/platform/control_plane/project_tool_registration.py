"""Owner submission for an immutable project search tool runtime."""
import hashlib
import json
import re
from typing import Literal
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ai_tool_reviews import review_id, require_review
from .models import AIToolReview, AuditEvent
from .project_routing import resolve_project
from .workspace_gateway import call_files


ENGINE = 'project-http-v1'
FILES = ('Dockerfile', 'app.py', 'openapi.yaml', 'requirements.txt')
COMMAND = re.compile(r'(提交项目工具|授权项目工具) ([a-z][a-z0-9-]{0,39})')


def call_limit(snapshot, settings):
    if snapshot and snapshot.get('program', {}).get('protocol') == 2:
        return settings.project_web_calls
    return min(3, settings.agent_mcp_calls)


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=180)
    recent_days: int = Field(default=30, ge=1, le=30, strict=True)

    @field_validator('query')
    @classmethod
    def public_query(cls, value):
        if (any(ord(c) < 32 for c in value)
                or any(word.startswith(('!', ':')) for word in value.split())
                or re.search(r'(?i)https?://|\b(?:bearer|api[_ -]?key|password|token)\s*[:=]|sk-[a-z0-9_-]{12,}', value)):
            raise ValueError('project_tool_public_query_required')
        return value


class WebQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    operation: Literal['search', 'read']
    query: str | None = Field(default=None, min_length=2, max_length=180)
    period: Literal['day', 'week', 'month', 'year'] | None = None
    url: str | None = Field(default=None, max_length=1000)
    focus: str | None = Field(default=None, max_length=180)
    offset: int = Field(default=0, ge=0, le=200000, strict=True)

    @model_validator(mode='after')
    def validate_operation(self):
        if self.operation == 'search':
            if not self.query or self.url is not None or self.focus is not None or self.offset:
                raise ValueError('project_tool_search_arguments')
            Query.public_query(self.query)
        else:
            if not self.url or self.query is not None or self.period is not None:
                raise ValueError('project_tool_read_arguments')
            from .public_sources import public_url
            self.url = public_url(self.url)
        return self


def command(messages):
    if isinstance(messages, list) and messages and isinstance(last := messages[-1], dict):
        if last.get('role') == 'user' and isinstance(value := last.get('content'), str):
            return COMMAND.fullmatch(value.strip())
    return None


def artifact(db, principal, settings, project, directory):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', directory):
        raise HTTPException(422, 'project_tool_invalid_directory')
    files = {}
    protocol = 1
    for name in FILES:
        content = call_files(db, principal, settings, 'GET', '/v1/file',
                             f'{project.project_path}/tools/{directory}/{name}').content
        if not 0 < len(content) <= 131072:
            raise HTTPException(422, 'project_tool_invalid_artifact')
        files[name] = hashlib.sha256(content).hexdigest()
        if name == 'openapi.yaml':
            try:
                manifest = json.loads(content)
                if manifest.get('x-starbridge-protocol') == 'search-read-v2':
                    protocol = 2
            except (ValueError, AttributeError):
                pass  # Legacy YAML manifests keep their frozen v1 protocol.
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return digest, files, protocol


async def runtime_request(db, settings, snapshot, arguments, environment_id):
    from .project_tool_runtime import execute as run
    return await run(db, settings, snapshot, arguments, environment_id)


async def execute(settings, snapshot, arguments, *, db=None, environment_id=None):
    if not settings.project_tool_registration_enabled:
        raise ValueError('project_tool_disabled')
    if snapshot.get('program', {}).get('protocol') == 2:
        query = WebQuery.model_validate(arguments)
        result = await runtime_request(db, settings, snapshot, query.model_dump(exclude_none=True), environment_id)
        # The reviewed artifact performs extraction; the control plane only bounds the transport.
        if not isinstance(result, dict) or result.get('operation') != query.operation:
            raise ValueError('project_tool_invalid_output')
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > 11000 or len(text.encode()) > 16000:
            raise ValueError('project_tool_invalid_output')
        return text
    query = Query.model_validate(arguments)
    result = await runtime_request(db, settings, snapshot, query.model_dump(), environment_id)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    cutoff = today - timedelta(days=query.recent_days)
    sources = []
    from .public_sources import public_url
    for item in result.get('sources', [])[:3]:
        try:
            date_field = 'published_on' if item.get('published_on') is not None else 'content_on'
            source_date = date.fromisoformat(item[date_field])
            url = public_url(item['url'])
            excerpt = item['excerpt']
            if cutoff <= source_date <= today and isinstance(excerpt, str) and len(excerpt) >= 100:
                source = {'url': url, date_field: source_date.isoformat(), 'excerpt': excerpt[:4100]}
                if isinstance(title := item.get('title'), str):
                    source['title'] = title[:160]
                if isinstance(read_at := item.get('read_at'), str):
                    timestamp = datetime.fromisoformat(read_at.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    if timestamp.tzinfo and now - timedelta(minutes=5) <= timestamp <= now + timedelta(seconds=30):
                        source['read_at'] = timestamp.isoformat()
                sources.append(source)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return json.dumps({'status': 'sources_read' if sources else 'no_recent_readable_sources',
                       'sources': sources, 'external_content_is_untrusted': True}, ensure_ascii=False)


async def handle(db, env, binding, settings, principal, action, directory):
    from .ai_trials import draft_snapshot, save_draft
    from .generated_tools import reference_for

    env_id, project_id = env.id, binding.project_id
    kind, _ = resolve_project(db, env_id, project_id)
    if kind != 'assistant' or env.status in ('deleted', 'suspended'):
        raise HTTPException(404, 'project_tool_unavailable')
    from .models import WorkspaceProject
    digest, files, protocol = artifact(db, principal, settings, db.get(WorkspaceProject, project_id), directory)
    snapshot = {'engine': ENGINE, 'name': 'project_web_search', 'title': '项目联网搜索 · '+directory,
                'requirements': '搜索并读取近期公开网页原文。', 'source_digest': digest,
                'project_id': project_id, 'directory': directory, 'files': files,
                'cases': [], 'program': {'parameters': ['query', 'recent_days']}}
    if protocol == 2:
        snapshot['program'] = {'protocol': 2, 'parameters': ['operation', 'query', 'period', 'url', 'focus', 'offset']}
        snapshot['requirements'] = '搜索发现候选网页；由模型选择来源，单独读取原文并按需补查。公开网页只读。'
    reference = reference_for(snapshot)
    identifier = review_id(project_id, reference)
    db.rollback()
    try:
        from .project_tool_runtime import context
        context(db, settings, snapshot, env_id)
    except ValueError:
        return '当前项目工具尚无可验证的构建产物或工作区。请先完成构建，再提交审核。'
    db.refresh(env, with_for_update=True)
    db.refresh(binding, with_for_update=True)
    if env.status in ('deleted', 'suspended') or binding.state != 'ready' or binding.environment_id != env_id:
        raise HTTPException(404, 'project_tool_unavailable')
    if action == '授权项目工具':
        try:
            require_review(db, project_id, reference, identifier, lock=True)
        except HTTPException:
            return '这个工具版本还未通过审核，暂时无法授权。'
        _, config, before = draft_snapshot(db, binding, settings)
        save_draft(db, env, binding, settings, before.digest(), config.model_copy(update={'tool_reference': reference}))
        return '已授权当前审核版本并保存到智能体草稿。打开试用即可调用。'
    row = db.get(AIToolReview, identifier)
    if row is None:
        db.add(AIToolReview(id=identifier, project_id=project_id, reference=reference, state='pending', revision=1,
                           descriptor={'name': snapshot['name'], 'title': snapshot['title'],
                                       'description': snapshot['requirements'], 'target': '项目私有运行服务 · 公开网页只读',
                                       'parameters': snapshot['program']['parameters'], 'generated': snapshot}))
        db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=principal.issuer, subject=principal.subject,
                          action='ai_generated_tool.review_requested', target_id=identifier))
        db.commit()
        return '项目工具已提交审核。管理员通过后可授权该版本。'
    return {'pending': '该版本正在等待审核。', 'approved': '该版本已通过审核，可授权。',
            'rejected': '该版本未通过审核。', 'revoked': '该版本审核已撤销。'}[row.state]
