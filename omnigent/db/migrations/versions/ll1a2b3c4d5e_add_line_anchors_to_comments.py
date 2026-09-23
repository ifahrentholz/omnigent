"""Add line anchors and diff side to review comments.

Revision ID: ll1a2b3c4d5e
Revises: kk1a2b3c4d5e
Create Date: 2026-09-23 00:00:00.000000

``start_line``/``end_line`` (1-based, inclusive) and ``side`` (``"before"`` or
``"after"``) record where in the file, and on which side of a diff, a comment
was made, so the agent receives ``path:L12-14`` instead of character offsets.
All ``NULL`` for existing comments.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ll1a2b3c4d5e"
down_revision: str | None = "kk1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable anchor columns."""
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
