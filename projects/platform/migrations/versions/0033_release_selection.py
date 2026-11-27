"""Separate a creator's release from the version shared with members."""
from alembic import op
import sqlalchemy as sa

revision = '0033_release_selection'
down_revision = '0032_ai_tool_reviews'
branch_labels = depends_on = None


def upgrade():
    op.add_column('ai_releases', sa.Column('description', sa.String(512)))
    op.add_column('ai_applications', sa.Column('shared_release_id', sa.String(36)))
    op.execute('UPDATE ai_applications SET shared_release_id = active_release_id')


def downgrade():
    db = op.get_bind()
    if (db.scalar(sa.text('SELECT count(*) FROM ai_releases WHERE description IS NOT NULL')) or
            db.scalar(sa.text('SELECT count(*) FROM ai_applications WHERE '
                "coalesce(shared_release_id, '') <> coalesce(active_release_id, '')"))):
        raise RuntimeError('Release metadata and version selections require forward recovery')
    op.drop_column('ai_applications', 'shared_release_id')
    op.drop_column('ai_releases', 'description')
