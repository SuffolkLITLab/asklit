"""Schema initialization is an explicit deployment step, never an API side effect."""

import argparse
from pathlib import Path
from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db"])
    parser.parse_args()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    print("Hosted database migrated to the latest schema.")


if __name__ == "__main__":
    main()
