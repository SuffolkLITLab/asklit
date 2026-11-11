"""Organization opt-in and daily allowance for paid speech."""
from alembic import op
import sqlalchemy as sa
revision = "e519a407b112"
down_revision = "d42c9081a031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hosted_organizations", sa.Column("paid_voice_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("hosted_organizations", sa.Column("paid_voice_daily_calls", sa.Integer(), nullable=False, server_default="100"))


def downgrade():
    op.drop_column("hosted_organizations", "paid_voice_daily_calls")
    op.drop_column("hosted_organizations", "paid_voice_enabled")
