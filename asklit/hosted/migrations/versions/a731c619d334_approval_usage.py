"""Operator approval and durable per-call usage accounting."""
from alembic import op
import sqlalchemy as sa
revision = 'a731c619d334'
down_revision = 'f620b508c223'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_users', sa.Column('author_approved', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('hosted_users', sa.Column('platform_admin', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('hosted_usage_events',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('created',sa.Float(),nullable=False),
        sa.Column('org_id',sa.String(32),sa.ForeignKey('hosted_organizations.id'),nullable=False),
        sa.Column('bot_id',sa.String(32),sa.ForeignKey('hosted_bots.id'),nullable=False),
        sa.Column('owner_id',sa.String(32),sa.ForeignKey('hosted_users.id'),nullable=False),
        sa.Column('purpose',sa.String(40),nullable=False),
        sa.Column('preset',sa.String(100),nullable=False),
        sa.Column('model',sa.String(300),nullable=False),
        sa.Column('status',sa.String(20),nullable=False),
        sa.Column('input_tokens',sa.Integer(),nullable=True),
        sa.Column('output_tokens',sa.Integer(),nullable=True),
        sa.Column('input_rate',sa.Float(),nullable=True),
        sa.Column('output_rate',sa.Float(),nullable=True),
        sa.Column('estimated_cost_usd',sa.Float(),nullable=True),
        sa.Column('latency_ms',sa.Integer(),nullable=True))
    for field in ['created','org_id','bot_id','owner_id']:
        op.create_index('ix_hosted_usage_events_'+field,'hosted_usage_events',[field])

def downgrade():
    op.drop_table('hosted_usage_events')
    op.drop_column('hosted_users','platform_admin')
    op.drop_column('hosted_users','author_approved')
