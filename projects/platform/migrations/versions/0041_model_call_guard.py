"""Add admission reservations, without touching wallets or historical usage."""
from alembic import op
import sqlalchemy as sa

revision = '0041_model_call_guard'
down_revision = '0040_daily_free'
branch_labels = depends_on = None


def upgrade():
    op.create_table('model_call_reservations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('environment_id', sa.String(36), sa.ForeignKey('user_environments.id'), nullable=False),
        sa.Column('window_id', sa.String(80), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('key_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('limit_nanos', sa.BigInteger(), nullable=False),
        sa.Column('reserved_nanos', sa.BigInteger(), nullable=False),
        sa.Column('settled_nanos', sa.BigInteger()),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('usage', sa.JSON(none_as_null=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('dispatched','settled','blocked')"),
        sa.CheckConstraint('limit_nanos > 0 AND reserved_nanos > 0'),
        sa.CheckConstraint("(state = 'settled' AND settled_nanos >= 0 AND usage IS NOT NULL) OR state <> 'settled'"))
    op.create_index('ix_model_call_budget_owner', 'model_call_reservations', ['environment_id', 'window_id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM model_call_reservations')):
        raise RuntimeError('Retain model admission and receipt history')
    op.drop_table('model_call_reservations')
