"""Protect hosted tables from Supabase Data API roles and record email provenance."""

from alembic import op
import sqlalchemy as sa

revision = "b9d215ed1a01"
down_revision = "a5fe04e9e446"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "hosted_identities",
        sa.Column(
            "email_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("hosted_identities", "email_verified", server_default=None)
    # SQL-created tables in Supabase may inherit grants to Data API roles.
    # Backend DB credentials must own these tables (or have BYPASSRLS). Clients
    # must go through FastAPI authorization, never query hosted tables directly.
    from asklit.hosted.migrations.privacy import PRIVACY_SQL

    if op.get_bind().dialect.name == "postgresql":
        op.execute(PRIVACY_SQL)


def downgrade():
    # Keep RLS and revoked grants: rolling back email metadata must not expose data.
    op.drop_column("hosted_identities", "email_verified")
