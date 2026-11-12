"""Operator-issued school invitations include hosting approval."""
from alembic import op
import sqlalchemy as sa
revision = 'da64f9420667'
down_revision = 'c953e831f556'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_invitations',sa.Column('recipient_email',sa.String(320),nullable=True))
    op.add_column('hosted_invitations',sa.Column('grants_hosting',sa.Boolean(),nullable=False,server_default=sa.false()))

def downgrade():
    op.drop_column('hosted_invitations','grants_hosting')
    op.drop_column('hosted_invitations','recipient_email')
