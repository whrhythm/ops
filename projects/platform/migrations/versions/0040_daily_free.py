"""Account-local daily model allowances. No wallet or price changes."""
from alembic import op
import sqlalchemy as sa

revision = '0040_daily_free'
down_revision = '0039_purchase_rules'
branch_labels = depends_on = None


def upgrade():
    op.create_table('daily_free_buckets',
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), primary_key=True),
        sa.Column('day', sa.String(10), primary_key=True),
        sa.Column('allowance', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('allowance > 0'))
    op.create_table('daily_free_calls',
        sa.Column('environment_id', sa.String(36), primary_key=True),
        sa.Column('attempt_id', sa.String(36), primary_key=True),
        sa.Column('day', sa.String(10), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('input_bound', sa.BigInteger(), nullable=False),
        sa.Column('output_bound', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('input_tokens', sa.BigInteger()),
        sa.Column('output_tokens', sa.BigInteger()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['environment_id','day'], ['daily_free_buckets.environment_id','daily_free_buckets.day']),
        sa.CheckConstraint("state IN ('reserved','dispatched','settled','released')"),
        sa.CheckConstraint('input_bound >= 0 AND output_bound > 0'),
        sa.CheckConstraint("(state = 'settled' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL "
                           "AND input_tokens >= 0 AND output_tokens >= 0) OR "
                           "(state <> 'settled' AND input_tokens IS NULL AND output_tokens IS NULL)"))
    op.create_index('ix_daily_free_period','daily_free_calls',['environment_id','day'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM daily_free_buckets')):
        raise RuntimeError('Retain daily allowance and attempt history')
    op.drop_table('daily_free_calls')
    op.drop_table('daily_free_buckets')
