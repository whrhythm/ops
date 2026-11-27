"""E2 invitations and versioned company files, scoped to an active membership."""
from base64 import b64decode
from binascii import Error as Base64Error
from datetime import timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accounts import IdentityClient, require_access
from .auth import Principal
from .database import get_db
from .enterprises import Input, memberships, require_enterprises, utc, view
from .models import AuditEvent, Enterprise, EnterpriseAsset, EnterpriseAssetRevision, EnterpriseEntitlement
from .models import EnterpriseInvitation, EnterpriseMembership, now
from .settings import get_settings

REVISION = '0036_enterprise_collaboration'
FILE_BYTES = 1024 * 1024
TRIAL_BYTES = 64 * FILE_BYTES
FILE_COUNT = 100
VERSION_COUNT = 100
Role = Literal['viewer', 'editor']


class Invite(Input):
    username: str = Field(min_length=1, max_length=120, pattern=r'^[^\s/\\]+$')
    role: Role


class FileWrite(Input):
    request_id: UUID
    content: str = Field(min_length=4, max_length=4 * ((FILE_BYTES + 2) // 3))

    def decode(self) -> bytes:
        try:
            data = b64decode(self.content, validate=True)
        except (Base64Error, ValueError):
            raise HTTPException(422, 'enterprise_file_invalid_encoding') from None
        if not 0 < len(data) <= FILE_BYTES:
            raise HTTPException(413, 'enterprise_file_size_limit')
        return data


class CreateFile(FileWrite):
    name: str = Field(min_length=1, max_length=255)

    @field_validator('name')
    @classmethod
    def file_name(cls, name: str) -> str:
        if not name.isprintable() or '/' in name or '\\' in name or name in {'.', '..'}:
            raise ValueError('invalid file name')
        return name


class SaveFile(FileWrite):
    base: int = Field(ge=1, le=VERSION_COUNT)


def require_collaboration():
    require_enterprises()
    if not get_settings().enterprise_collaboration_enabled:
        raise HTTPException(404, 'enterprise_collaboration_unavailable')


def record(db: Session, principal: Principal, enterprise: str, action: str, target: str):
    db.add(AuditEvent(tenant_id=enterprise, target_id=target, issuer=principal.issuer,
                      subject=principal.subject, action=f'enterprise.{action}'))


def lock_enterprise(db: Session, enterprise_id: str, *, write: bool):
    row = db.scalar(select(Enterprise).where(Enterprise.id == enterprise_id).with_for_update(read=not write))
    if row is None:
        raise HTTPException(404, 'enterprise_missing')
    return row


def access(db: Session, principal: Principal, enterprise_id: str, *, write=False, owner=False, revoking=False):
    # Check first to avoid locking an enterprise on behalf of a foreign account.
    query = memberships(principal).where(Enterprise.id == enterprise_id)
    if db.execute(query).one_or_none() is None:
        raise HTTPException(404, 'enterprise_missing')
    lock_enterprise(db, enterprise_id, write=write or revoking)
    row = db.execute(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise HTTPException(404, 'enterprise_missing')
    enterprise, member, entitlement = row
    if (owner and member.role != 'owner') or (write and member.role not in {'owner', 'editor'}):
        raise HTTPException(403, 'enterprise_access_denied')
    if member.role not in {'owner', 'editor', 'viewer'}:
        raise HTTPException(403, 'enterprise_access_denied')
    if write and utc(entitlement.expires_at) <= now():
        raise HTTPException(403, 'enterprise_trial_expired')
    return enterprise, member, entitlement


def invite_state(invite: EnterpriseInvitation) -> str:
    return 'expired' if invite.status == 'pending' and utc(invite.expires_at) <= now() else invite.status


def invitation_view(invite, name):
    return {'id': invite.id, 'enterprise_name': name, 'username': invite.label, 'role': invite.role,
            'status': invite_state(invite), 'expires_at': utc(invite.expires_at)}


def resolve_recipient(username: str) -> tuple[str, str]:
    data = IdentityClient(get_settings()).call('GET', 'core/users/', params={'username': username, 'page_size': 2})
    matches = [item for item in data.get('results', []) if item.get('username') == username and item.get('is_active')]
    if len(matches) == 1:
        uid = matches[0].get('uid')
        if isinstance(uid, str) and 0 < len(uid) <= 255 and uid.isprintable():
            return uid, username
    raise HTTPException(404, 'enterprise_invitee_unavailable')


def file_for(db, enterprise_id, asset_id):
    asset = db.scalar(select(EnterpriseAsset).where(EnterpriseAsset.enterprise_id == enterprise_id,
                                                    EnterpriseAsset.id == asset_id))
    if asset is None:
        raise HTTPException(404, 'enterprise_file_missing')
    return asset


def file_view(asset):
    return {'id': asset.id, 'name': asset.name, 'head': asset.head}


def used_storage(db, enterprise_id):
    return db.scalar(select(func.coalesce(func.sum(EnterpriseAssetRevision.size), 0))
                     .join(EnterpriseAsset).where(EnterpriseAsset.enterprise_id == enterprise_id))


def save_revision(db, principal, member, entitlement, asset, body, data, number):
    limit = min(TRIAL_BYTES, entitlement.limits['storage_gib'] * 1024 ** 3)
    if used_storage(db, asset.enterprise_id) + len(data) > limit:
        raise HTTPException(507, 'enterprise_storage_limit')
    db.add(EnterpriseAssetRevision(asset_id=asset.id, number=number, request_id=str(body.request_id),
                                   author=member.id, size=len(data), digest=sha256(data).hexdigest(), content=data))
    if number != asset.head:
        asset.head = number
    record(db, principal, asset.enterprise_id, 'file_saved', asset.id)
    db.commit()
    return file_view(asset)


def matched_revision(db, asset, member, request_id, data, expected_number):
    revision = db.scalar(select(EnterpriseAssetRevision).where(EnterpriseAssetRevision.asset_id == asset.id,
                         EnterpriseAssetRevision.request_id == str(request_id)))
    if revision and (revision.author != member.id or revision.number != expected_number or
                     revision.digest != sha256(data).hexdigest()):
        raise HTTPException(409, 'enterprise_request_conflict')
    return revision is not None


def create_router(service_principal):
    router = APIRouter(prefix='/internal/v1/workspace/enterprise', tags=['enterprise collaboration'],
                       dependencies=[Depends(require_collaboration)])

    def user(principal: Principal = Depends(service_principal), db: Session = Depends(get_db)):
        require_access(db, principal)
        return principal

    @router.get('/invitations')
    def inbox(principal=Depends(user), db: Session = Depends(get_db)):
        rows = db.execute(select(EnterpriseInvitation, Enterprise.name).join(Enterprise)
                          .join(EnterpriseEntitlement, EnterpriseEntitlement.enterprise_id == Enterprise.id)
                          .where(EnterpriseInvitation.issuer == principal.issuer,
                                 EnterpriseInvitation.subject == principal.subject,
                                 EnterpriseInvitation.status == 'pending', EnterpriseInvitation.expires_at > now(),
                                 EnterpriseEntitlement.expires_at > now())
                          .order_by(EnterpriseInvitation.created_at.desc()).limit(100)).all()
        return [invitation_view(invite, name) for invite, name in rows]

    @router.get('/{enterprise_id}/members')
    def members(enterprise_id: UUID, principal=Depends(user), db: Session = Depends(get_db)):
        enterprise, _, _ = access(db, principal, str(enterprise_id), owner=True)
        rows = db.scalars(select(EnterpriseInvitation).where(EnterpriseInvitation.enterprise_id == enterprise.id)
                          .order_by(EnterpriseInvitation.created_at.desc()).limit(1000))
        return [invitation_view(invite, enterprise.name) for invite in rows]

    @router.post('/{enterprise_id}/invite')
    def invite(enterprise_id: UUID, body: Invite, principal=Depends(user), db: Session = Depends(get_db)):
        enterprise, _, _ = access(db, principal, str(enterprise_id), owner=True)
        # Close the read transaction before the identity provider network request.
        key = enterprise.id
        db.rollback()
        target, label = resolve_recipient(body.username)
        require_access(db, Principal(principal.issuer, target))
        enterprise, _, entitlement = access(db, principal, key, owner=True, write=True)
        member = db.scalar(select(EnterpriseMembership).filter_by(enterprise_id=key, issuer=principal.issuer, subject=target))
        if member and member.active:
            raise HTTPException(409, 'enterprise_already_member')
        active = EnterpriseInvitation.status == 'pending', EnterpriseInvitation.expires_at > now()
        pending = db.scalar(select(EnterpriseInvitation).where(EnterpriseInvitation.enterprise_id == key,
                            EnterpriseInvitation.issuer == principal.issuer, EnterpriseInvitation.subject == target, *active))
        if pending:
            if pending.role != body.role:
                raise HTTPException(409, 'enterprise_invite_conflict')
            return invitation_view(pending, enterprise.name)
        seats = db.scalar(select(func.count()).select_from(EnterpriseMembership).where(
            EnterpriseMembership.enterprise_id == key, EnterpriseMembership.active.is_(True)))
        reserved = db.scalar(select(func.count()).select_from(EnterpriseInvitation).where(
            EnterpriseInvitation.enterprise_id == key, *active))
        if seats + reserved >= entitlement.limits['seats']:
            raise HTTPException(409, 'enterprise_seats_full')
        row = EnterpriseInvitation(enterprise_id=key, issuer=principal.issuer, subject=target, label=label,
                                 role=body.role, expires_at=min(now() + timedelta(days=7), utc(entitlement.expires_at)))
        db.add(row)
        db.flush()
        record(db, principal, key, 'member_invited', row.id)
        db.commit()
        return invitation_view(row, enterprise.name)

    @router.post('/invitations/{invitation_id}/accept')
    def accept(invitation_id: UUID, principal=Depends(user), db: Session = Depends(get_db)):
        query = select(EnterpriseInvitation).filter_by(id=str(invitation_id), issuer=principal.issuer,
                                                       subject=principal.subject)
        row = db.scalar(query)
        if row is None:
            raise HTTPException(404, 'enterprise_invitation_missing')
        enterprise = lock_enterprise(db, row.enterprise_id, write=True)
        row = db.scalar(query.execution_options(populate_existing=True))
        entitlement = db.get(EnterpriseEntitlement, enterprise.id)
        if not entitlement or utc(entitlement.expires_at) <= now() or invite_state(row) not in {'pending', 'accepted'}:
            raise HTTPException(404, 'enterprise_invitation_missing')
        member = db.scalar(select(EnterpriseMembership).filter_by(enterprise_id=enterprise.id,
                           issuer=principal.issuer, subject=principal.subject))
        if row.status == 'accepted':
            if not member or not member.active:
                raise HTTPException(404, 'enterprise_invitation_missing')
            return view(enterprise, member, entitlement)
        occupied = db.scalar(select(func.count()).select_from(EnterpriseMembership).where(
            EnterpriseMembership.enterprise_id == enterprise.id, EnterpriseMembership.active.is_(True)))
        if (member and member.active) or occupied >= entitlement.limits['seats']:
            raise HTTPException(409, 'enterprise_seats_full')
        if member:
            member.active, member.role = True, row.role
        else:
            member = EnterpriseMembership(enterprise_id=enterprise.id, issuer=principal.issuer,
                                          subject=principal.subject, role=row.role)
            db.add(member)
        row.status = 'accepted'
        record(db, principal, enterprise.id, 'member_accepted', row.id)
        db.commit()
        return view(enterprise, member, entitlement)

    @router.post('/{enterprise_id}/invitations/{invitation_id}/revoke')
    def revoke(enterprise_id: UUID, invitation_id: UUID, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        # Revocation must remain possible after the trial expires.
        access(db, principal, key, owner=True, revoking=True)
        row = db.scalar(select(EnterpriseInvitation).filter_by(id=str(invitation_id), enterprise_id=key))
        if row is None:
            raise HTTPException(404, 'enterprise_invitation_missing')
        if row.status != 'revoked':
            if row.status == 'accepted':
                member = db.scalar(select(EnterpriseMembership).filter_by(enterprise_id=key,
                                   issuer=row.issuer, subject=row.subject))
                if member and member.role != 'owner':
                    member.active = False
            row.status = 'revoked'
            record(db, principal, key, 'member_revoked', row.id)
            db.commit()
        return {'id': row.id, 'status': row.status}

    @router.get('/{enterprise_id}/files')
    def files(enterprise_id: UUID, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        access(db, principal, key)
        return [file_view(row) for row in db.scalars(select(EnterpriseAsset).filter_by(enterprise_id=key)
                                                     .order_by(EnterpriseAsset.name_key))]

    @router.post('/{enterprise_id}/files')
    def create_file(enterprise_id: UUID, body: CreateFile, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        _, member, entitlement = access(db, principal, key, write=True)
        data = body.decode()
        same = db.scalar(select(EnterpriseAsset).filter_by(enterprise_id=key, name_key=body.name.casefold()))
        if same:
            if matched_revision(db, same, member, body.request_id, data, 1):
                return file_view(same)
            raise HTTPException(409, 'enterprise_file_name_conflict')
        if db.scalar(select(func.count()).select_from(EnterpriseAsset).filter_by(enterprise_id=key)) >= FILE_COUNT:
            raise HTTPException(507, 'enterprise_file_count_limit')
        asset = EnterpriseAsset(enterprise_id=key, created_by=member.id, name=body.name, name_key=body.name.casefold())
        db.add(asset)
        db.flush()
        return save_revision(db, principal, member, entitlement, asset, body, data, 1)

    @router.get('/{enterprise_id}/files/{asset_id}/versions')
    def versions(enterprise_id: UUID, asset_id: UUID, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        access(db, principal, key)
        asset = file_for(db, key, str(asset_id))
        rows = db.scalars(select(EnterpriseAssetRevision).filter_by(asset_id=asset.id)
                          .order_by(EnterpriseAssetRevision.number.desc()))
        return [{'number': row.number, 'size': row.size, 'sha256': row.digest, 'created_at': utc(row.created_at)} for row in rows]

    @router.post('/{enterprise_id}/files/{asset_id}/versions')
    def edit_file(enterprise_id: UUID, asset_id: UUID, body: SaveFile, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        _, member, entitlement = access(db, principal, key, write=True)
        asset = file_for(db, key, str(asset_id))
        data = body.decode()
        if matched_revision(db, asset, member, body.request_id, data, body.base + 1):
            return file_view(asset)
        if asset.head != body.base:
            raise HTTPException(409, 'enterprise_file_version_conflict')
        if asset.head >= VERSION_COUNT:
            raise HTTPException(507, 'enterprise_file_version_limit')
        return save_revision(db, principal, member, entitlement, asset, body, data, asset.head + 1)

    @router.get('/{enterprise_id}/files/{asset_id}/versions/{number}', response_class=Response)
    def content(enterprise_id: UUID, asset_id: UUID, number: int, principal=Depends(user), db: Session = Depends(get_db)):
        key = str(enterprise_id)
        access(db, principal, key)
        asset = file_for(db, key, str(asset_id))
        row = db.get(EnterpriseAssetRevision, (asset.id, number))
        if row is None:
            raise HTTPException(404, 'enterprise_file_version_missing')
        data = row.content
        if len(data) != row.size or sha256(data).hexdigest() != row.digest:
            raise HTTPException(503, 'enterprise_file_unavailable')
        return Response(content=data, media_type='application/octet-stream', headers={
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'attachment'})

    return router
