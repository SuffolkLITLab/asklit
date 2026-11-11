"""Private conversation attachments."""
from alembic import op
import sqlalchemy as sa

revision = "d42c9081a031"
down_revision = "c31d7629c120"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hosted_attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("hosted_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(180), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
    )
    op.create_index("ix_hosted_attachments_conversation_id", "hosted_attachments", ["conversation_id"])


def downgrade():
    op.drop_table("hosted_attachments")
