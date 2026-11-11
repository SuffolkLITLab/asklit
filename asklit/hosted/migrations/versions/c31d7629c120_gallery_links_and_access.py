"""Stable organization links, galleries and per-AskLit access controls."""

from alembic import op
import sqlalchemy as sa

revision = "c31d7629c120"
down_revision = "b9d215ed1a01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "hosted_identities",
        sa.Column("hosted_domain", sa.String(255), nullable=False, server_default=""),
    )
    for col in [
        sa.Column("slug", sa.String(80), nullable=True),
        sa.Column(
            "gallery_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("allowed_domains", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("microsoft_tenants", sa.JSON(), nullable=False, server_default="[]"),
    ]:
        op.add_column("hosted_organizations", col)
    for col in [
        sa.Column("slug", sa.String(80), nullable=True),
        sa.Column(
            "access_mode", sa.String(20), nullable=False, server_default="unlisted"
        ),
        sa.Column("access_version", sa.Integer(), nullable=False, server_default="1"),
    ]:
        op.add_column("hosted_bots", col)
    op.add_column(
        "hosted_conversations",
        sa.Column("access_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "hosted_conversations", sa.Column("access_grant", sa.String(64), nullable=True)
    )
    op.create_table(
        "hosted_organization_aliases",
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(32),
            sa.ForeignKey("hosted_organizations.id"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_hosted_organization_aliases_org_id",
        "hosted_organization_aliases",
        ["org_id"],
    )
    op.create_table(
        "hosted_bot_aliases",
        sa.Column(
            "org_id",
            sa.String(32),
            sa.ForeignKey("hosted_organizations.id"),
            primary_key=True,
        ),
        sa.Column("slug", sa.String(80), primary_key=True),
        sa.Column(
            "bot_id", sa.String(32), sa.ForeignKey("hosted_bots.id"), nullable=False
        ),
    )
    op.create_index("ix_hosted_bot_aliases_bot_id", "hosted_bot_aliases", ["bot_id"])
    op.create_table(
        "hosted_password_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "bot_id", sa.String(32), sa.ForeignKey("hosted_bots.id"), nullable=False
        ),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("lookup", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    for column in ["bot_id", "lookup"]:
        op.create_index(
            "ix_hosted_password_keys_" + column, "hosted_password_keys", [column]
        )
    op.create_table(
        "hosted_access_grants",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "bot_id", sa.String(32), sa.ForeignKey("hosted_bots.id"), nullable=False
        ),
        sa.Column("access_version", sa.Integer(), nullable=False),
        sa.Column(
            "password_id",
            sa.String(32),
            sa.ForeignKey("hosted_password_keys.id"),
            nullable=True,
        ),
        sa.Column("password_version", sa.Integer(), nullable=True),
        sa.Column(
            "user_id", sa.String(32), sa.ForeignKey("hosted_users.id"), nullable=True
        ),
        sa.Column("session_hash", sa.String(64), nullable=True),
        sa.Column("expires", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_hosted_access_grants_bot_id", "hosted_access_grants", ["bot_id"]
    )
    # Both historical and canonical names stay reserved to their original owner.
    connection = op.get_bind()
    for row in connection.execute(
        sa.text("SELECT id FROM hosted_organizations")
    ).mappings():
        slug = "org-" + row["id"]
        connection.execute(
            sa.text("UPDATE hosted_organizations SET slug=:slug WHERE id=:id"),
            {"slug": slug, "id": row["id"]},
        )
        connection.execute(
            sa.text(
                "INSERT INTO hosted_organization_aliases (slug,org_id) VALUES (:slug,:id)"
            ),
            {"slug": slug, "id": row["id"]},
        )
    for row in connection.execute(
        sa.text("SELECT id,org_id FROM hosted_bots")
    ).mappings():
        slug = "asklit-" + row["id"]
        connection.execute(
            sa.text("UPDATE hosted_bots SET slug=:slug WHERE id=:id"),
            {"slug": slug, "id": row["id"]},
        )
        connection.execute(
            sa.text(
                "INSERT INTO hosted_bot_aliases (org_id,slug,bot_id) VALUES (:org,:slug,:id)"
            ),
            {"org": row["org_id"], "slug": slug, "id": row["id"]},
        )
    if connection.dialect.name == "postgresql":
        from asklit.hosted.migrations.privacy import PRIVACY_SQL

        op.execute(PRIVACY_SQL)


def downgrade():
    for table in [
        "hosted_access_grants",
        "hosted_password_keys",
        "hosted_bot_aliases",
        "hosted_organization_aliases",
    ]:
        op.drop_table(table)
    for table, columns in [
        ("hosted_conversations", ["access_grant", "access_version"]),
        ("hosted_bots", ["slug", "access_mode", "access_version"]),
        (
            "hosted_organizations",
            ["slug", "gallery_enabled", "allowed_domains", "microsoft_tenants"],
        ),
        ("hosted_identities", ["hosted_domain"]),
    ]:
        for column in columns:
            op.drop_column(table, column)
