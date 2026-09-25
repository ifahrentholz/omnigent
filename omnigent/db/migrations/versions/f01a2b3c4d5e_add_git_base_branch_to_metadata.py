"""Add the worktree base branch to Omnigent conversation metadata.

Revision ID: f01a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-23 00:00:00.000000

``git_base_branch`` records the ref a server-created worktree branch forked
from, so clients can diff a task's changes against it. ``NULL`` for sessions
without a created worktree and for worktrees created before this column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f01a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``git_base_branch`` column."""
    columns = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "git_base_branch" in columns:
        return
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("git_base_branch", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Drop the ``git_base_branch`` column."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("git_base_branch")
