"""what was asked of the suppliers, and when their prices came in

Two halves of the same screen, and they live apart on purpose.

    rfq_inquiries               the letter that went to one supplier, whole
    rfq_lines.offer_received_at when that supplier's price arrived

The letter is kept as text rather than as a template plus its arguments. It is
editable before it goes, so the sentence that was actually sent is the one
worth keeping - not the one a later version of the template would rebuild from
the same lines. `lines` is the indexes it asked about, as JSONB rather than a
join table: nothing is ever queried by it, and a letter is read whole or not
at all.

One row per supplier per RFQ, enforced. Two letters to the same firm about the
same delivery is the mistake the grouping exists to prevent, and a unique
constraint is the only place it cannot arrive by a different door.

`offer_received_at` sits beside the price rather than on the inquiry, because a
supplier answers about lines: a reply covering two lines of three would date
the third one too if the moment lived on the letter.

Revision ID: a71f3c5d9e04
Revises: d4a9c07e2b58
Create Date: 2026-09-25 12:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a71f3c5d9e04"
down_revision: str | Sequence[str] | None = "d4a9c07e2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rfq_inquiries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email_id", sa.String(length=64), nullable=False),
        sa.Column("supplier", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Every table carries these two, from `Base`. The database's clock
        # rather than the service's, so a row written by psql or a migration
        # is stamped the same way as one written by the API.
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
            name=op.f("rfq_inquiries_email_id_fkey"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("rfq_inquiries_pkey")),
        sa.UniqueConstraint("email_id", "supplier", name="rfq_inquiries_email_id_supplier_key"),
    )
    op.create_index(op.f("rfq_inquiries_email_id_idx"), "rfq_inquiries", ["email_id"], unique=False)

    # Null for every line there is: nobody has been asked yet, so no price has
    # arrived - and a backfilled timestamp would date replies that never came.
    op.add_column(
        "rfq_lines", sa.Column("offer_received_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("rfq_lines", "offer_received_at")
    op.drop_index(op.f("rfq_inquiries_email_id_idx"), table_name="rfq_inquiries")
    op.drop_table("rfq_inquiries")
