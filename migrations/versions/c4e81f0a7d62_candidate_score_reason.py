"""why a candidate scored what it did, and room for "not scored"

    rfq_line_candidates.why         what the score rests on, in one sentence
    rfq_line_candidates.confidence  now nullable

A candidate's confidence used to be a word count and always had a value. It is
now worked out from what a model observed about the candidate, and a line the
model did not answer for has no score - which is not the same as a score of
zero, and a zero would read as "certainly not" on the screen. So the column
takes null.

Existing rows keep the number they have and an empty reason: they were scored
the old way, and a reason written for them now would be invented.

Revision ID: c4e81f0a7d62
Revises: b93e17a4c250
Create Date: 2026-09-27 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e81f0a7d62"
down_revision: str | Sequence[str] | None = "b93e17a4c250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The server default only fills the rows already there; the model supplies
    # the value from here on, so the default goes again at once - as it has
    # none in `models.py`.
    op.add_column(
        "rfq_line_candidates",
        sa.Column("why", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.alter_column("rfq_line_candidates", "why", server_default=None)
    op.alter_column(
        "rfq_line_candidates", "confidence", existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    # A null had no way to be written before this revision; zero is the closest
    # the old column can hold.
    op.execute("UPDATE rfq_line_candidates SET confidence = 0 WHERE confidence IS NULL")
    op.alter_column(
        "rfq_line_candidates", "confidence", existing_type=sa.Integer(), nullable=False
    )
    op.drop_column("rfq_line_candidates", "why")
