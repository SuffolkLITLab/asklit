"""Administrator-managed production local accounts."""
from alembic import op
import sqlalchemy as sa
revision = "f620b508c223"
down_revision = "e519a407b112"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hosted_local_accounts",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("hosted_users.id"), primary_key=True),
        sa.Column("username", sa.String(320), nullable=False, unique=True),
        sa.Column("managed_org_id", sa.String(32), sa.ForeignKey("hosted_organizations.id"), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index("ix_hosted_local_accounts_managed_org_id", "hosted_local_accounts", ["managed_org_id"])


def downgrade():
    op.drop_table("hosted_local_accounts")
