"""Durable office execution receipts; existing files and billing tables unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0037_office_tasks'
down_revision = '0036_enterprise_collaboration'
branch_labels = depends_on = None


def upgrade():
    op.create_table('office_tasks',
        sa.Column('id', sa.String(36), sa.ForeignKey('code_executions.id'), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('conversation_id', sa.String(128), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_office_tasks_environment_id', 'office_tasks', ['environment_id'])
    op.create_index('ix_office_tasks_conversation_id', 'office_tasks', ['conversation_id'])
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql' and bind.scalar(sa.text("SELECT 1 FROM pg_roles WHERE rolname='platform_api'")):
        op.execute('GRANT SELECT, INSERT, UPDATE ON office_tasks TO platform_api')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM office_tasks')):
        raise RuntimeError('Office task receipts must be retained; disable the feature instead of deleting task history')
    op.drop_table('office_tasks')
