"""Add line anchors and diff side to review comments.

Revision ID: f02a2b3c4d5e
Revises: f01a2b3c4d5e
Create Date: 2026-09-23 00:00:00.000000

``start_line``/``end_line`` (1-based, inclusive) and ``side`` (``"before"`` or
``"after"``) record where in the file, and on which side of a diff, a comment
was made, so the agent receives ``path:L12-14`` instead of character offsets.
All ``NULL`` for existing comments. Idempotent, like ``f01a2b3c4d5e``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f02a2b3c4d5e"
down_revision: str | None = "f01a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable anchor columns."""
    if "side" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("comments")}:
        return
    with op.batch_alter_table("comments") as batch_op:
        batch_op.add_column(sa.Column("start_line", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("end_line", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("side", sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Drop the anchor columns."""
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_column("side")
        batch_op.drop_column("end_line")
        batch_op.drop_column("start_line")
