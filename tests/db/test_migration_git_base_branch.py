"""Migration coverage for the worktree base branch on conversation metadata."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PREVIOUS_REVISION = "jj1a2b3c4d5e"
_REVISION = "kk1a2b3c4d5e"
_TABLE = "omnigent_conversation_metadata"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
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


def _columns(engine: sa.Engine) -> dict[str, dict]:
    """
    Read the metadata table's columns by name.

    :param engine: Engine to inspect.
    :returns: Column info keyed by name.
    """
    return {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}


def test_git_base_branch_migration_round_trips(tmp_path: Path) -> None:
    """
    The column is added nullable, existing rows read NULL, and downgrade
    drops it without touching other metadata.

    :param tmp_path: Pytest temporary directory.
    """
    uri = f"sqlite:///{tmp_path / 'git-base-branch.db'}"
    engine = sa.create_engine(uri)
    conv_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    try:
        _run(uri, engine, "upgrade", _PREVIOUS_REVISION)
        assert "git_base_branch" not in _columns(engine)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, created_at, updated_at, root_conversation_id, title) "
                    "VALUES (0, :id, 1, 2, :id, 'Original')"
                ),
                {"id": conv_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata "
                    "(workspace_id, id, kind, host_id, workspace, git_branch) "
                    "VALUES (0, :id, 1, :id, '/w/repo-worktrees/x', 'omni/x')"
                ),
                {"id": conv_id},
            )

        _run(uri, engine, "upgrade", _REVISION)
        assert _columns(engine)["git_base_branch"]["nullable"] is True
        with engine.begin() as connection:
            row = connection.execute(
                sa.text(f"SELECT git_branch, git_base_branch FROM {_TABLE}")
            ).one()
            assert row.git_branch == "omni/x"
            assert row.git_base_branch is None
            connection.execute(sa.text(f"UPDATE {_TABLE} SET git_base_branch = 'main'"))

        _run(uri, engine, "downgrade", _PREVIOUS_REVISION)
        assert "git_base_branch" not in _columns(engine)
        with engine.begin() as connection:
            assert connection.scalar(sa.text(f"SELECT git_branch FROM {_TABLE}")) == "omni/x"
    finally:
        engine.dispose()
