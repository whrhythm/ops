"""Short-lived, BFF-verified construction capabilities; no user data rewrite."""
from alembic import op
import sqlalchemy as sa

revision = '0034_authoring_catalog'
down_revision = '0033_release_selection'
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('ai_authoring_catalogs'):
        # An explicitly staged, additive expansion can precede the final rollout.
        if {c['name'] for c in inspector.get_columns('ai_authoring_catalogs')} != {'project_id', 'encrypted_content', 'updated_at'}:
            raise RuntimeError('Unexpected authoring catalogue schema')
        return
    op.create_table('ai_authoring_catalogs',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('ai_project_bindings.project_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('encrypted_content', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    # Rebuildable short-lived catalogue, never the draft or an authorization record.
    op.drop_table('ai_authoring_catalogs')
