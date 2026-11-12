"""Reserve model spend before provider calls."""
from alembic import op
import sqlalchemy as sa
revision = 'b842d720e445'
down_revision = 'a731c619d334'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_usage_events', sa.Column('budget_reserved_usd', sa.Float(), nullable=True))

def downgrade():
    op.drop_column('hosted_usage_events', 'budget_reserved_usd')
