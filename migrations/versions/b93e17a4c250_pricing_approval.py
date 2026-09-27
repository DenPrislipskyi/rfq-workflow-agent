"""what this RFQ sells for, frozen the moment somebody approved it

Two halves of one sign-off.

    rfq_approvals                 that it happened, when, and with which margins
    rfq_lines.approved_unit_price what each line sells for, per unit

The prices are **kept, not recomputed**, and that is the whole point of
approving. Cost and margin both move afterwards - a supplier requotes, the
sheet is edited, somebody types a different margin - and a quotation that
quietly followed them would stop being the number the customer was given.

`rfq_approvals` is one row per email at most, like `email_verdicts` beside it.
Its presence is the whole of "this RFQ is approved": there is no flag next to
it to disagree with, and no way back. The two margins live here rather than on
every line because they are one decision about the whole quotation - they
answer "why is this line priced so" once instead of repeating themselves down
each row.

`Numeric` for the margins for the same reason as for the prices: a margin that
rounds differently on two machines prices the quotation differently on two
machines.

Revision ID: b93e17a4c250
Revises: a71f3c5d9e04
Create Date: 2026-09-25 16:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b93e17a4c250"
down_revision: str | Sequence[str] | None = "a71f3c5d9e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rfq_approvals",
        sa.Column("email_id", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("margin_stock", sa.Numeric(7, 3), nullable=False),
        sa.Column("margin_jit", sa.Numeric(7, 3), nullable=False),
        # Every table carries these two, from `Base`.
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["emails.id"],
            name=op.f("rfq_approvals_email_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("email_id", name=op.f("rfq_approvals_pkey")),
    )

    # Null for every line there is: nothing has been approved yet, and a
    # backfilled price would be a quotation nobody signed.
    op.add_column("rfq_lines", sa.Column("approved_unit_price", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("rfq_lines", "approved_unit_price")
    op.drop_table("rfq_approvals")
