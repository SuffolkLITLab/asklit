"""Hosted schema migrations; no changes to standalone SQLite databases."""

from alembic import context
from sqlalchemy import text
from asklit.hosted.migrations.privacy import PRIVACY_SQL
from asklit.hosted.models import Base, Store
from asklit.hosted.settings import Settings

url = Settings.from_env().database_url


def include_object(obj, name, type_, reflected, compare_to):
    # A Supabase database can contain unrelated application tables. Never propose
    # dropping or altering those during AskLit migration autogeneration.
    return name.startswith("hosted_") if type_ == "table" else True


if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        version_table="asklit_schema_version",
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = Store(url).engine
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table="asklit_schema_version",
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
            if connection.dialect.name == "postgresql":
                connection.execute(text(PRIVACY_SQL))
