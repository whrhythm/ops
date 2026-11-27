"""Immutable, administrator-reviewed office skill packages and scoped runtime access."""
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import AIConfiguration, AuditEvent, UserEnvironment, now
from .settings import get_settings


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
    commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    directory: str = Field(min_length=1, max_length=200)


def safe_path(value):
    return (bool(re.fullmatch(r'[A-Za-z0-9_.\-/]+', value)) and
            not value.startswith('/') and all(p not in ('', '.', '..') for p in value.split('/')))


class SkillPackage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Source
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,79}$')
    description: str = Field(min_length=1, max_length=2000)
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    files: dict[str, str] = Field(min_length=2, max_length=16)
    permissions: list[Literal['selected-files.read', 'python.execute']] = Field(min_length=1, max_length=2)
    environment_ids: list[UUID] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def validate_contents(self):
        if not safe_path(self.source.directory):
            raise ValueError('Invalid source directory')
        if len(set(self.environment_ids)) != len(self.environment_ids) or len(set(self.permissions)) != len(self.permissions):
            raise ValueError('Duplicate scope')
        if 'SKILL.md' not in self.files or 'LICENSE.md' not in self.files:
            raise ValueError('SKILL.md and LICENSE.md are required')
        total = 0
        for filename, content in self.files.items():
            if not safe_path(filename) or PurePosixPath(filename).suffix != '.md':
                raise ValueError('Only reviewable Markdown resources are supported')
            size = len(content.encode('utf-8'))
            total += size
            if size > 65536 or '\x00' in content:
                raise ValueError('Invalid resource size or content')
            for target in re.findall(r'\[[^\]\n]*\]\(([^\s)]+)\)', content):
                target = target.split('#')[0]
                if not target or re.match(r'^https?://', target):
                    continue
                resolved = str(PurePosixPath(filename).parent / target)
                if not safe_path(resolved) or resolved not in self.files:
                    raise ValueError('Unresolved local resource reference')
        if total > 262144:
            raise ValueError('Package exceeds review limit')
        if not re.search(r'^name:\s*' + re.escape(self.name) + r'\s*$', self.files['SKILL.md'], re.M):
            raise ValueError('Skill name must match SKILL.md')
        return self


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['approve', 'reject', 'install', 'revoke']
    acknowledged: Literal[True]


def package_digest(package):
    encoded = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_enabled():
    if not get_settings().office_skills_enabled:
        raise HTTPException(404, 'office_skills_unavailable')


def review_out(row):
    return {'id': row.id, 'revision': row.revision, **row.config}


def audit(db, principal, action, identifier):
    db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                      action='office_skill.' + action, target_id=identifier))


def admin_router(admin_dependency):
    router = APIRouter(prefix='/v1/admin/office-skills', tags=['office skills'], dependencies=[Depends(require_enabled)])

    @router.get('')
    def listing(principal=Depends(admin_dependency), db=Depends(get_db)):
        rows = db.scalars(select(AIConfiguration).where(AIConfiguration.id.startswith('skill-'))
                          .order_by(AIConfiguration.observed_at.desc()).limit(100))
        return {'reviews': [review_out(row) for row in rows]}

    @router.post('')
    def submit(body: SkillPackage, principal=Depends(admin_dependency), db=Depends(get_db)):
        package = body.model_dump(mode='json')
        for identifier in body.environment_ids:
            if not db.get(UserEnvironment, str(identifier)):
                raise HTTPException(422, 'office_skill_account_not_found')
        digest = package_digest(package)
        identifier = 'skill-' + digest[:26]
        row = db.get(AIConfiguration, identifier)
        if row:
            if row.config['digest'] != digest:
                raise HTTPException(409, 'office_skill_identity_conflict')
            return review_out(row)
        row = AIConfiguration(id=identifier, revision=1, observed_at=now(), config={
            'digest': digest, 'state': 'pending', 'package': package,
            'submitted_by': principal.subject, 'decisions': [],
        }, catalog=[])
        db.add(row)
        audit(db, principal, 'submitted', identifier)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(AIConfiguration, identifier)
            if not row or row.config['digest'] != digest:
                raise HTTPException(409, 'office_skill_identity_conflict')
        return review_out(row)

    @router.post('/{identifier}/decision')
    def decide(identifier: str, body: Decision, principal=Depends(admin_dependency), db=Depends(get_db)):
        row = db.get(AIConfiguration, identifier, with_for_update=True, populate_existing=True)
        if not identifier.startswith('skill-') or not row:
            raise HTTPException(404, 'office_skill_not_found')
        config = row.config
        if (row.revision != body.revision or config['digest'] != body.digest or
                package_digest(config['package']) != body.digest):
            raise HTTPException(409, 'office_skill_changed')
        transitions = {('pending', 'approve'): 'approved', ('pending', 'reject'): 'rejected',
                       ('approved', 'install'): 'installed', ('approved', 'revoke'): 'revoked',
                       ('installed', 'revoke'): 'revoked'}
        state = transitions.get((config['state'], body.action))
        if state is None:
            raise HTTPException(409, 'office_skill_transition_refused')
        if state == 'installed':
            for environment in sorted(config['package']['environment_ids']):
                if not db.get(UserEnvironment, environment, with_for_update=True):
                    raise HTTPException(409, 'office_skill_account_not_found')
            active = db.scalars(select(AIConfiguration).where(AIConfiguration.id.startswith('skill-')))
            for other in active:
                other_config = other.config
                if (other.id != row.id and other_config['state'] == 'installed' and
                        other_config['package']['name'] == config['package']['name'] and
                        set(other_config['package']['environment_ids']) & set(config['package']['environment_ids'])):
                    raise HTTPException(409, 'office_skill_name_already_installed')
        decision = {'action': body.action, 'by': principal.subject, 'at': now().isoformat(),
                    'digest': body.digest, 'revision': body.revision}
        updated = {**config, 'state': state, 'decisions': [*config['decisions'], decision]}
        result = db.execute(update(AIConfiguration).where(AIConfiguration.id == identifier,
                            AIConfiguration.revision == body.revision)
                            .values(config=updated, revision=body.revision + 1, observed_at=now()))
        if result.rowcount != 1:
            raise HTTPException(409, 'office_skill_changed')
        audit(db, principal, body.action, identifier)
        db.commit()
        db.refresh(row)
        return review_out(row)

    return router


def runtime_router(principal_dependency):
    router = APIRouter(prefix='/internal/v1/workspace/office-skills', tags=['office skills'],
                       dependencies=[Depends(require_enabled)])

    @router.get('')
    def catalog(principal=Depends(principal_dependency), db=Depends(get_db)):
        environment = db.scalar(select(UserEnvironment).where(UserEnvironment.issuer == principal.issuer,
                                                             UserEnvironment.subject == principal.subject))
        if not environment:
            return {'skills': []}
        rows = db.scalars(select(AIConfiguration).where(AIConfiguration.id.startswith('skill-')))
        result = []
        for row in rows:
            config = row.config
            if config['state'] != 'installed' or environment.id not in config['package']['environment_ids']:
                continue
            if package_digest(config['package']) != config['digest']:
                raise HTTPException(503, 'office_skill_integrity_failed')
            result.append({'id': row.id, 'revision': row.revision, 'digest': config['digest'],
                           'package': config['package']})
        return {'skills': result}

    return router
