"""Enterprise invitations and immutable shared file revisions."""
from alembic import op
import sqlalchemy as sa

revision = '0036_enterprise_collaboration'
down_revision = '0035_enterprise_opening'
branch_labels = depends_on = None


def upgrade():
    op.create_table('enterprise_invitations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('enterprise_id', sa.String(36), sa.ForeignKey('enterprises.id'), nullable=False),
        sa.Column('issuer', sa.String(512), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('label', sa.String(120), nullable=False),
        sa.Column('role', sa.String(24), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_enterprise_invitations_enterprise_id', 'enterprise_invitations', ['enterprise_id'])
    op.create_index('ix_enterprise_invitations_recipient', 'enterprise_invitations', ['issuer', 'subject'])
    op.create_table('enterprise_assets',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('enterprise_id', sa.String(36), sa.ForeignKey('enterprises.id'), nullable=False),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('enterprise_memberships.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('name_key', sa.String(768), nullable=False),
        sa.Column('head', sa.Integer(), nullable=False),
        sa.UniqueConstraint('enterprise_id', 'name_key'))
    op.create_table('enterprise_asset_revisions',
        sa.Column('asset_id', sa.String(36), sa.ForeignKey('enterprise_assets.id'), primary_key=True),
        sa.Column('number', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('author', sa.String(36), sa.ForeignKey('enterprise_memberships.id'), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('asset_id', 'request_id'))
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql' and bind.scalar(sa.text("SELECT 1 FROM pg_roles WHERE rolname='platform_api'")):
        op.execute('GRANT SELECT, INSERT, UPDATE (status) ON enterprise_invitations TO platform_api')
        op.execute('GRANT UPDATE (active, role) ON enterprise_memberships TO platform_api')
        op.execute('GRANT SELECT, INSERT, UPDATE (head) ON enterprise_assets TO platform_api')
        op.execute('GRANT SELECT, INSERT ON enterprise_asset_revisions TO platform_api')


def downgrade():
    bind = op.get_bind()
    for table in ('enterprise_invitations', 'enterprise_assets'):
        if bind.scalar(sa.text(f'SELECT COUNT(*) FROM {table}')):
            raise RuntimeError('Preserve enterprise records and file history; roll back the application instead')
    if bind.dialect.name == 'postgresql' and bind.scalar(sa.text("SELECT 1 FROM pg_roles WHERE rolname='platform_api'")):
        op.execute('REVOKE UPDATE (active, role) ON enterprise_memberships FROM platform_api')
    op.drop_table('enterprise_asset_revisions')
    op.drop_table('enterprise_assets')
    op.drop_table('enterprise_invitations')
