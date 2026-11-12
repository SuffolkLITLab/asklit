"""Allow operators to remove pending requests without deleting account history."""
from alembic import op
import sqlalchemy as sa
revision = 'c953e831f556'
down_revision = 'b842d720e445'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_users', sa.Column('approval_deleted', sa.Boolean(), nullable=False, server_default=sa.false()))

def downgrade():
    op.drop_column('hosted_users', 'approval_deleted')
