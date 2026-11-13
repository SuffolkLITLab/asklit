"""Keep instructor-only material outside model and publication configuration."""
from alembic import op
import sqlalchemy as sa
revision = 'fc861b642889'
down_revision = 'eb750a531778'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('hosted_bots',sa.Column('instructor_notes',sa.Text(),nullable=False,server_default=''))
    op.add_column('hosted_bots',sa.Column('instructor_password_hash',sa.Text(),nullable=False,server_default=''))

def downgrade():
    op.drop_column('hosted_bots','instructor_password_hash')
    op.drop_column('hosted_bots','instructor_notes')
