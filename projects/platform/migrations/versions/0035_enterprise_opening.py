"""Enterprise applications, membership and trial entitlements."""
from alembic import op
import sqlalchemy as sa

revision = '0035_enterprise_opening'
down_revision = '0034_authoring_catalog'
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        'enterprise_applications',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('issuer', sa.String(512), nullable=False),
        sa.Column('applicant_subject', sa.String(255), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('name_key', sa.String(360), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('owner_subject', sa.String(255)),
        sa.Column('code_digest', sa.String(64), unique=True),
        sa.Column('code_expires_at', sa.DateTime(timezone=True)),
        sa.Column('trial_days', sa.Integer()),
        sa.Column('limits', sa.JSON()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('issuer', 'applicant_subject', 'name_key', name='uq_enterprise_application_name'),
    )
    op.create_table(
        'enterprises',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('application_id', sa.String(36), sa.ForeignKey('enterprise_applications.id'), nullable=False, unique=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('setup_step', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'enterprise_memberships',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('enterprise_id', sa.String(36), sa.ForeignKey('enterprises.id'), nullable=False),
        sa.Column('issuer', sa.String(512), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('role', sa.String(24), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('enterprise_id', 'issuer', 'subject', name='uq_enterprise_member'),
    )
    op.create_table(
        'enterprise_entitlements',
        sa.Column('enterprise_id', sa.String(36), sa.ForeignKey('enterprises.id'), primary_key=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('limits', sa.JSON(), nullable=False),
    )
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql' and bind.scalar(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'platform_api'")
    ):
        op.execute('GRANT SELECT, INSERT, UPDATE ON enterprise_applications, enterprises TO platform_api')
        op.execute('GRANT SELECT, INSERT ON enterprise_memberships, enterprise_entitlements TO platform_api')


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text('SELECT COUNT(*) FROM enterprise_applications')):
        raise RuntimeError('Preserve enterprise records; roll back the application with enterprise disabled')
    op.drop_table('enterprise_entitlements')
    op.drop_table('enterprise_memberships')
    op.drop_table('enterprises')
    op.drop_table('enterprise_applications')
