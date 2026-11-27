"""Additive shadow cost console. Does not alter wallets, prices or debit paths."""
from alembic import op
import sqlalchemy as sa

revision = '0038_cost_console'
down_revision = '0037_office_tasks'
branch_labels = depends_on = None


def upgrade():
    op.create_table('purchase_rates',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('channel', sa.String(128), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('input_per_million', sa.String(40), nullable=False),
        sa.Column('output_per_million', sa.String(40), nullable=False),
        sa.Column('cached_per_million', sa.String(40)),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('note', sa.String(500), nullable=False),
        sa.Column('test_only', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('channel', 'model', 'effective_at', name='uq_purchase_rate_start'))
    op.create_table('cost_receipts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(255), nullable=False),
        sa.Column('channel', sa.String(128), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('native_spend', sa.String(50)),
        sa.Column('rate_id', sa.String(36), sa.ForeignKey('purchase_rates.id')),
        sa.Column('cost', sa.String(50)),
        sa.Column('currency', sa.String(3)),
        sa.Column('usage_sent_at', sa.DateTime(timezone=True)),
        sa.Column('cost_sent_at', sa.DateTime(timezone=True)),
        sa.Column('first_send_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.String(60)),
        sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False))
    for field in ('owner_id', 'channel', 'model', 'occurred_at'):
        op.create_index('ix_cost_receipts_' + field, 'cost_receipts', [field])
    op.create_table('cost_sync_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('report', sa.JSON(), nullable=False))
    if op.get_bind().dialect.name == 'postgresql' and op.get_bind().scalar(sa.text(
            "SELECT 1 FROM pg_roles WHERE rolname='platform_api'")):
        op.execute('GRANT SELECT, INSERT, UPDATE ON purchase_rates, cost_receipts, cost_sync_state TO platform_api')


def downgrade():
    for table in ('cost_receipts', 'purchase_rates', 'cost_sync_state'):
        if op.get_bind().scalar(sa.text('SELECT count(*) FROM ' + table)):
            raise RuntimeError('Retain cost evidence: disable the feature; do not delete its history')
    for table in ('cost_receipts', 'purchase_rates', 'cost_sync_state'):
        op.drop_table(table)
