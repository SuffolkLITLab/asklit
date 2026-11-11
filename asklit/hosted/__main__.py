"""Explicit operator commands; no public bootstrap or migration endpoints."""
import argparse
import getpass
from pathlib import Path
from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    operator = commands.add_parser("set-operator")
    operator.add_argument("--user-id", required=True)
    enable = commands.add_parser("enable-local-accounts")
    enable.add_argument("--organization-id", required=True)
    admin = commands.add_parser("create-local-admin")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    admin.add_argument("--organization", required=True)
    admin.add_argument("--slug", default="lit")
    admin.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    if args.command == "init-db":
        command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
        print("Hosted database migrated to the latest schema.")
    elif args.command == "set-operator":
        from .models import Store, User
        from .settings import Settings
        with Store(Settings.from_env().database_url).transaction() as db:
            user = db.get(User, args.user_id)
            if not user: parser.error("Account not found.")
            user.platform_admin = True
            user.author_approved = True
        print("Account granted service operator access and hosting approval.")
    elif args.command == "enable-local-accounts":
        from .models import Store, Organization
        from .settings import Settings
        with Store(Settings.from_env().database_url).transaction() as db:
            org = db.get(Organization, args.organization_id)
            if not org: parser.error("Organization not found.")
            org.local_accounts_enabled = True
        print("Organization administrators can now create local accounts.")
    else:
        from .models import Store
        from .settings import Settings
        from .local_accounts import bootstrap_admin
        password = args.password_file.read_text().rstrip("\r\n") if args.password_file else getpass.getpass("Initial password: ")
        try:
            bootstrap_admin(Store(Settings.from_env().database_url), args.email, args.name, args.organization, args.slug, password)
        except ValueError as error:
            parser.error(str(error))
        print("Local owner created. Sign in with the supplied email and password.")


if __name__ == "__main__":
    main()
