"""Versioned supplier procurement rules without changing user billing tables."""
from alembic import op
import sqlalchemy as sa

revision = '0039_purchase_rules'
down_revision = '0038_cost_console'
branch_labels = depends_on = None


def upgrade():
    op.add_column('purchase_rates', sa.Column('pricing_rule', sa.JSON(), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM purchase_rates WHERE pricing_rule IS NOT NULL')):
        raise RuntimeError('Retain configured supplier pricing rules')
    op.drop_column('purchase_rates', 'pricing_rule')
