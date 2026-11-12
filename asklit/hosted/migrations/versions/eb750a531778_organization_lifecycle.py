"""Organization archives and anonymous retained budget totals."""
from alembic import op
import sqlalchemy as sa
revision = 'eb750a531778'
down_revision = 'da64f9420667'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_organizations', sa.Column('archived',sa.Boolean(),nullable=False,server_default=sa.false()))
    op.create_table('hosted_pending_object_deletions',sa.Column('key',sa.String(100),primary_key=True))
    op.create_table('hosted_deleted_organization_spend',sa.Column('month',sa.String(7),primary_key=True),sa.Column('amount',sa.Float(),nullable=False))

def downgrade():
    op.drop_table('hosted_pending_object_deletions')
    op.drop_table('hosted_deleted_organization_spend')
    op.drop_column('hosted_organizations','archived')
