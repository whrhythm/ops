"""Managed free/paid admission and explicit balance consent. Preserve old ledgers."""
from alembic import op
import sqlalchemy as sa

revision='0042_chat_release'
down_revision='0041_model_call_guard'
branch_labels=depends_on=None


def upgrade():
    conn=op.get_bind()
    checks=sa.inspect(conn).get_check_constraints('model_call_reservations')
    state=next(c for c in checks if 'state' in c['sqltext'] and 'blocked' in c['sqltext'])
    if conn.dialect.name=='sqlite':
        table=sa.Table('model_call_reservations',sa.MetaData(),autoload_with=conn)
        for constraint in list(table.constraints):
            if isinstance(constraint,sa.CheckConstraint) and 'blocked' in str(constraint.sqltext):table.constraints.remove(constraint)
        with op.batch_alter_table('model_call_reservations',copy_from=table) as batch:
            batch.create_check_constraint('ck_model_call_states',"state IN ('reserved','dispatched','settled','released','blocked')")
    else:
        op.drop_constraint(state['name'],'model_call_reservations',type_='check')
        op.create_check_constraint('ck_model_call_states','model_call_reservations',"state IN ('reserved','dispatched','settled','released','blocked')")
    op.create_table('chat_balance_consents',
        sa.Column('environment_id',sa.String(36),sa.ForeignKey('user_environments.id'),primary_key=True),
        sa.Column('day',sa.String(10),primary_key=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))


def downgrade():
    conn=op.get_bind()
    if conn.scalar(sa.text('SELECT count(*) FROM chat_balance_consents')) or conn.scalar(sa.text("SELECT count(*) FROM model_call_reservations WHERE state IN ('reserved','released')")):
        raise RuntimeError('Retain consent and admission history; use the compatible reader rollback')
    op.drop_table('chat_balance_consents')
    with op.batch_alter_table('model_call_reservations') as batch:
        batch.drop_constraint('ck_model_call_states',type_='check')
        batch.create_check_constraint('ck_model_call_states',"state IN ('dispatched','settled','blocked')")
