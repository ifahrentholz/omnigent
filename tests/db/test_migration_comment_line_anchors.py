"""Migration coverage for line anchors on review comments."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PREVIOUS_REVISION = "f01a2b3c4d5e"
_REVISION = "f02a2b3c4d5e"


def _migrate(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    """
    Run an alembic upgrade or downgrade on ``engine``.

    :param uri: Database URI used to build the alembic config.
    :param engine: Engine whose connection the migration runs on.
    :param action: ``"upgrade"`` or ``"downgrade"``.
    :param revision: Target revision id.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


def test_comment_line_anchor_migration_round_trips(tmp_path: Path) -> None:
    """
    The anchor columns are added nullable and dropped again cleanly.

    :param tmp_path: Pytest temporary directory.
    """
    uri = f"sqlite:///{tmp_path / 'comment-anchors.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, "upgrade", _REVISION)
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns("comments")}
        for name in ("start_line", "end_line", "side"):
            assert columns[name]["nullable"] is True
        _migrate(uri, engine, "downgrade", _PREVIOUS_REVISION)
        names = {c["name"] for c in sa.inspect(engine).get_columns("comments")}
        assert not {"start_line", "end_line", "side"} & names
    finally:
        engine.dispose()
