"""Private construction plans. A plan records proposals, never grants capabilities."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .agent_tasks import seal, unseal
from .models import AIAuthoringCatalog, AuditEvent


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Method = Literal['reuse', 'existing_service', 'knowledge', 'skill', 'tool', 'mcp']
LABELS = {'reuse': '复用候选', 'existing_service': '接入已有服务', 'knowledge': '补充资料',
          'skill': '编写 Skill', 'tool': '开发工具', 'mcp': '接入或开发 MCP', 'clarify': '补充条件'}


class Need(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requirement: ShortText
    method: Method
    reference: str | None = Field(default=None, max_length=250)
    rationale: ShortText
    prerequisite: ShortText
    access_scope: ShortText

    @model_validator(mode='after')
    def validate_reference(self):
        if (self.method == 'reuse') != bool(self.reference):
            raise ValueError('reuse_requires_catalog_reference')
        return self


class PlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    goal: ShortText
    needs: list[Need] = Field(min_length=1, max_length=8)


def read_plan(db, project_id, settings):
    row = db.get(AIAuthoringCatalog, project_id)
    return unseal(row.encrypted_content, settings).get('project_plan') if row else None


def save_plan(db, env, binding, settings, data, directory):
    # Use the same environment lock as catalog synchronization and project deletion.
    row = db.get(AIAuthoringCatalog, binding.project_id, populate_existing=True)
    stored = unseal(row.encrypted_content, settings) if row else {}
    previous = stored.get('project_plan')
    if data.expected_revision != (previous['revision'] if previous else 0):
        return {'ok': False, 'code': 'plan_changed'}
    by_reference = {item['reference']: item for item in directory}
    if any(item.reference and item.reference not in by_reference for item in data.needs):
        return {'ok': False, 'code': 'capability_unavailable'}
    needs = [item.model_dump(exclude_none=True) for item in data.needs]
    for need in needs:
        if reference := need.get('reference'):
            need['capability_name'] = by_reference[reference]['name']
    plan = {'revision': data.expected_revision + 1, 'goal': data.goal, 'needs': needs}
    if not row:
        # A plan is durable even if the native catalog has never been synchronized.
        row = AIAuthoringCatalog(project_id=binding.project_id,
                                updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
        db.add(row)
    row.encrypted_content = seal({**stored, 'project_plan': plan}, settings)
    db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=env.issuer, subject=env.subject,
                      action='ai_project.plan_saved', target_id=binding.project_id))
    db.commit()
    return {'ok': True, 'plan': plan}


def display(plan):
    def literal(value):
        return ''.join('\\' + c if c in r'\`*_{}[]()<>#+-.!|~' else c
                       for c in ' '.join(value.split()))

    lines = [f"**已保存构建计划 · {literal(plan['goal'].rstrip('。'))}**", '']
    for need in plan['needs']:
        method = LABELS[need['method']]
        if name := need.get('capability_name'):
            method += '「' + literal(name) + '」'
        lines.append(f"- **{literal(need['requirement'])}** · {method}")
        rationale, prerequisite, scope = (literal(need[key].rstrip('。；'))
                                         for key in ('rationale', 'prerequisite', 'access_scope'))
        lines.extend([f'  {rationale}。  ', f'  待验证：{prerequisite}。  ', f'  访问范围：{scope}。', ''])
    return '\n\n' + '\n'.join(lines)
