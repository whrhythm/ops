"""Enterprise opening. Personal tenants, wallets and capacity remain independent."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .accounts import IdentityClient, require_access
from .auth import Principal
from .database import get_db
from .models import AuditEvent, Enterprise, EnterpriseApplication, EnterpriseEntitlement, EnterpriseMembership, now
from .settings import get_settings

REVISION = '0035_enterprise_opening'


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ApplicationInput(Input):
    name: str = Field(min_length=2, max_length=120)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.isprintable():
            raise ValueError('invalid enterprise name')
        return value


class Limits(Input):
    seats: int = Field(ge=1, le=10000)
    storage_gib: int = Field(ge=0, le=1000000)
    compute_minutes: int = Field(ge=0, le=1000000)


class IssueInput(Input):
    revision: int = Field(ge=1)
    owner_account_id: int | None = Field(default=None, ge=1)
    code_days: int = Field(ge=1, le=30)
    trial_days: int = Field(ge=1, le=90)
    limits: Limits


class RedeemInput(Input):
    code: SecretStr = Field(min_length=43, max_length=43)


class RevokeInput(Input):
    revision: int = Field(ge=1)


class SetupInput(Input):
    step: int = Field(ge=1, le=5)


class ApplicationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    status: Literal['pending', 'issued', 'revoked', 'activated']
    created_at: datetime
    code_expires_at: datetime | None


class IssueView(BaseModel):
    code: str
    expires_at: datetime


class AdminApplicationView(ApplicationView):
    applicant_subject: str
    revision: int


class EnterpriseView(BaseModel):
    id: str
    name: str
    role: str
    setup_step: int
    status: Literal['trial', 'expired']
    expires_at: datetime
    limits: Limits


class Overview(BaseModel):
    applications: list[ApplicationView]
    enterprises: list[EnterpriseView]


def require_enterprises():
    if not get_settings().enterprise_enabled:
        raise HTTPException(404, 'enterprise_unavailable')


def audit(db: Session, principal: Principal, target: str, action: str):
    db.add(AuditEvent(tenant_id=target, target_id=target, issuer=principal.issuer,
                      subject=principal.subject, action=f'enterprise.{action}'))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def view(enterprise: Enterprise, member: EnterpriseMembership, entitlement: EnterpriseEntitlement) -> EnterpriseView:
    expires_at = utc(entitlement.expires_at)
    return EnterpriseView(id=enterprise.id, name=enterprise.name, role=member.role,
                          setup_step=enterprise.setup_step, expires_at=expires_at, limits=entitlement.limits,
                          status='trial' if expires_at > now() else 'expired')


def memberships(principal: Principal):
    return (select(Enterprise, EnterpriseMembership, EnterpriseEntitlement)
            .join(EnterpriseMembership, EnterpriseMembership.enterprise_id == Enterprise.id)
            .join(EnterpriseEntitlement, EnterpriseEntitlement.enterprise_id == Enterprise.id)
            .where(EnterpriseMembership.issuer == principal.issuer,
                   EnterpriseMembership.subject == principal.subject, EnterpriseMembership.active.is_(True)))


def submit(db: Session, principal: Principal, name: str) -> EnterpriseApplication:
    query = select(EnterpriseApplication).filter_by(issuer=principal.issuer,
                                                     applicant_subject=principal.subject, name_key=name.casefold())
    existing = db.scalar(query)
    if existing is not None:
        return existing
    row = EnterpriseApplication(issuer=principal.issuer, applicant_subject=principal.subject,
                                name=name, name_key=name.casefold())
    db.add(row)
    try:
        db.flush()
        audit(db, principal, row.id, 'applied')
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(query)
        if existing is None:
            raise
        return existing
    return row


def issue(db: Session, principal: Principal, application_id: str, body: IssueInput) -> IssueView:
    row = db.get(EnterpriseApplication, application_id)
    if row is None:
        raise HTTPException(404, 'enterprise_application_missing')
    if row.status not in ('pending', 'revoked') or row.revision != body.revision:
        raise HTTPException(409, 'enterprise_application_changed')
    owner = row.applicant_subject
    if body.owner_account_id is not None:
        settings = get_settings()
        account = IdentityClient(settings).call('GET', f'core/users/{body.owner_account_id}/')
        owner = account.get('uid')
        if (not account.get('is_active') or not isinstance(owner, str) or not owner
                or len(owner) > 255 or not owner.isprintable() or row.issuer != settings.oidc_issuer):
            raise HTTPException(422, 'enterprise_owner_unavailable')
    require_access(db, Principal(row.issuer, owner))
    code = token_urlsafe(32)
    expires_at = now() + timedelta(days=body.code_days)
    result = db.execute(update(EnterpriseApplication)
                        .where(EnterpriseApplication.id == row.id, EnterpriseApplication.revision == body.revision)
                        .values(status='issued', owner_subject=owner, revision=body.revision + 1,
                                code_digest=sha256(code.encode()).hexdigest(), code_expires_at=expires_at,
                                trial_days=body.trial_days, limits=body.limits.model_dump()))
    if result.rowcount != 1:
        raise HTTPException(409, 'enterprise_application_changed')
    audit(db, principal, row.id, 'issued')
    db.commit()
    return IssueView(code=code, expires_at=expires_at)


def redeem(db: Session, principal: Principal, code: str) -> EnterpriseView:
    digest = sha256(code.encode()).hexdigest()
    query = select(EnterpriseApplication).where(EnterpriseApplication.code_digest == digest)
    row = db.scalar(query)
    if row is None or row.issuer != principal.issuer or row.owner_subject != principal.subject:
        raise HTTPException(404, 'enterprise_code_unavailable')
    if row.status == 'issued' and row.code_expires_at and utc(row.code_expires_at) > now():
        result = db.execute(update(EnterpriseApplication)
                            .where(EnterpriseApplication.id == row.id, EnterpriseApplication.status == 'issued',
                                   EnterpriseApplication.code_digest == digest,
                                   EnterpriseApplication.code_expires_at > now())
                            .values(status='activated', revision=EnterpriseApplication.revision + 1)
                            .execution_options(synchronize_session=False))
        if result.rowcount == 1:
            enterprise = Enterprise(application_id=row.id, name=row.name)
            db.add(enterprise)
            db.flush()
            member = EnterpriseMembership(enterprise_id=enterprise.id, issuer=principal.issuer,
                                          subject=principal.subject, role='owner')
            entitlement = EnterpriseEntitlement(enterprise_id=enterprise.id, limits=row.limits,
                                                expires_at=now() + timedelta(days=row.trial_days))
            db.add_all([member, entitlement])
            audit(db, principal, enterprise.id, 'activated')
            db.commit()
            return view(enterprise, member, entitlement)
        db.expire(row)
    if row.status == 'activated' and row.code_digest == digest:
        previous = db.execute(memberships(principal).where(Enterprise.application_id == row.id)).one_or_none()
        if previous is not None:
            return view(*previous)
    raise HTTPException(404, 'enterprise_code_unavailable')


def create_user_router(service_principal):
    router = APIRouter(prefix='/internal/v1/workspace/enterprise', tags=['enterprise'],
                       dependencies=[Depends(require_enterprises)])

    def user(principal: Principal = Depends(service_principal), db: Session = Depends(get_db)):
        require_access(db, principal)
        return principal

    @router.get('', response_model=Overview)
    def overview(principal: Principal = Depends(user), db: Session = Depends(get_db)):
        apps = db.scalars(select(EnterpriseApplication).where(
            EnterpriseApplication.issuer == principal.issuer,
            EnterpriseApplication.applicant_subject == principal.subject,
        ).order_by(EnterpriseApplication.created_at.desc())).all()
        spaces = db.execute(memberships(principal).order_by(Enterprise.created_at.desc())).all()
        return Overview(applications=[ApplicationView.model_validate(app) for app in apps],
                        enterprises=[view(*space) for space in spaces])

    @router.post('/apply', response_model=ApplicationView)
    def apply(body: ApplicationInput, principal: Principal = Depends(user), db: Session = Depends(get_db)):
        return submit(db, principal, body.name)

    @router.post('/activate', response_model=EnterpriseView)
    def activate(body: RedeemInput, principal: Principal = Depends(user), db: Session = Depends(get_db)):
        return redeem(db, principal, body.code.get_secret_value())

    @router.post('/{enterprise_id}/setup', response_model=EnterpriseView)
    def setup(enterprise_id: str, body: SetupInput, principal: Principal = Depends(user),
              db: Session = Depends(get_db)):
        row = db.execute(memberships(principal).where(Enterprise.id == enterprise_id)).one_or_none()
        if row is None:
            raise HTTPException(404, 'enterprise_missing')
        enterprise, member, entitlement = row
        if member.role != 'owner' or utc(entitlement.expires_at) <= now():
            raise HTTPException(403, 'enterprise_setup_denied')
        if body.step > enterprise.setup_step + 1:
            raise HTTPException(409, 'enterprise_setup_out_of_order')
        result = db.execute(update(Enterprise)
                            .where(Enterprise.id == enterprise.id, Enterprise.setup_step < body.step)
                            .values(setup_step=body.step))
        if result.rowcount == 1:
            audit(db, principal, enterprise.id, 'setup_saved')
            db.commit()
        db.refresh(enterprise)
        return view(enterprise, member, entitlement)

    return router


def create_admin_router(platform_admin):
    router = APIRouter(prefix='/v1/admin/enterprises', tags=['enterprise administration'],
                       dependencies=[Depends(require_enterprises), Depends(platform_admin)])

    @router.get('/applications', response_model=list[AdminApplicationView])
    def applications(page: int = Query(1, ge=1, le=100000), db: Session = Depends(get_db)):
        return db.scalars(select(EnterpriseApplication).order_by(
            EnterpriseApplication.created_at.desc(), EnterpriseApplication.id.desc(),
        ).offset((page - 1) * 25).limit(25)).all()

    @router.post('/applications/{application_id}/issue', response_model=IssueView)
    def issue_application(application_id: str, body: IssueInput, response: Response,
                          principal: Principal = Depends(platform_admin), db: Session = Depends(get_db)):
        response.headers['Cache-Control'] = 'no-store'
        return issue(db, principal, application_id, body)

    @router.post('/applications/{application_id}/revoke', status_code=204)
    def revoke(application_id: str, body: RevokeInput, principal: Principal = Depends(platform_admin),
               db: Session = Depends(get_db)):
        result = db.execute(update(EnterpriseApplication)
                            .where(EnterpriseApplication.id == application_id, EnterpriseApplication.status == 'issued',
                                   EnterpriseApplication.revision == body.revision)
                            .values(status='revoked', revision=body.revision + 1, code_digest=None, code_expires_at=None))
        if result.rowcount != 1:
            raise HTTPException(409, 'enterprise_application_changed')
        audit(db, principal, application_id, 'revoked')
        db.commit()

    return router
