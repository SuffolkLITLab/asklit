import sys
from types import SimpleNamespace

sys.modules.setdefault("litellm", SimpleNamespace())

from asklit.auth import hash_password
from asklit.scaffold import access


def test_no_configured_secret_leaves_the_scaffolder_open(monkeypatch):
    monkeypatch.setattr(access, "get_secret_value", lambda key, default=None: default)

    assert access.access_is_required() is False
    assert access.verify_scaffold_password("anything") is False


def test_a_plain_shared_password_is_compared_in_constant_time(monkeypatch):
    secrets = {"SCAFFOLD_PASSWORD": "class-2026"}
    monkeypatch.setattr(
        access, "get_secret_value", lambda key, default=None: secrets.get(key, default)
    )

    assert access.access_is_required() is True
    assert access.verify_scaffold_password("class-2026") is True
    assert access.verify_scaffold_password("class-2027") is False
    assert access.verify_scaffold_password("") is False
    assert access.verify_scaffold_password(None) is False


def test_a_hashed_password_is_preferred_over_a_plain_one(monkeypatch):
    secrets = {
        "SCAFFOLD_PASSWORD": "ignored-when-a-hash-exists",
        "SCAFFOLD_PASSWORD_HASH": hash_password("class-2026"),
    }
    monkeypatch.setattr(
        access, "get_secret_value", lambda key, default=None: secrets.get(key, default)
    )

    assert access.verify_scaffold_password("class-2026") is True
    assert access.verify_scaffold_password("ignored-when-a-hash-exists") is False


def test_dotted_secret_names_are_accepted(monkeypatch):
    secrets = {"scaffold.password": "class-2026"}
    monkeypatch.setattr(
        access, "get_secret_value", lambda key, default=None: secrets.get(key, default)
    )

    assert access.access_is_required() is True
    assert access.verify_scaffold_password("class-2026") is True


def test_blank_secret_values_do_not_enable_the_gate(monkeypatch):
    secrets = {"SCAFFOLD_PASSWORD": "   ", "SCAFFOLD_PASSWORD_HASH": ""}
    monkeypatch.setattr(
        access, "get_secret_value", lambda key, default=None: secrets.get(key, default)
    )

    assert access.access_is_required() is False
