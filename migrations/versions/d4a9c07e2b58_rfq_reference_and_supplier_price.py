"""the desk's number for an RFQ, and what a supplier quoted for a line

Two things the matching and sourcing screens need, and one counter between them.

    emails.rfq_reference        `RFQ-0042` - the desk's own number, and the only
                                identifier a customer or a supplier is shown
    rfq_lines.offer_unit_price  what a supplier quoted for one unit of a line

`rfq_reference` is deliberately not the primary key. `emails.id` addresses a
record the moment it is opened, whatever the email turns out to be, and one
email in five is not an RFQ at all - a supplier's reply, a portal notice, a
marketing blast. This is issued only once the verdict says RFQ, so the sequence
a customer counts has no gaps in it.

The numbering runs on rather than restarting each January, which is what lets
the year stay out of it: a number that restarts needs the year to tell two of
itself apart, and then every RFQ carries four digits identical for everyone all
year. A running counter is exactly what a sequence is, so that is what issues it.

Existing RFQs are numbered here, oldest first, so the column is not empty on a
database that already has records. The sequence is set to match, so the next
email carries on rather than colliding.

Revision ID: d4a9c07e2b58
Revises: 62632fefb6c4
Create Date: 2026-09-15 23:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a9c07e2b58"
down_revision: str | Sequence[str] | None = "62632fefb6c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# An RFQ's place in the whole run, oldest first. `id` breaks a tie, so the same
# database numbers the same way twice.
NUMBERED = """
    SELECT e.id,
           row_number() OVER (ORDER BY COALESCE(e.received_at, e.created_at), e.id)::int AS number
    FROM emails e
    JOIN email_verdicts v ON v.email_id = e.id
    WHERE v.is_rfq
"""


def upgrade() -> None:
    """Upgrade schema."""
    # Money, so `Numeric` rather than a float: money that rounds differently on
    # two machines is money somebody argues about. Null is nobody having
    # answered, which is not the same as a supplier quoting zero.
    op.add_column("rfq_lines", sa.Column("offer_unit_price", sa.Numeric(12, 2), nullable=True))

    # Unique because it goes out in a letter, and two RFQs carrying one number
    # are two RFQs nobody can tell apart.
    op.add_column("emails", sa.Column("rfq_reference", sa.String(32), nullable=True))
    op.create_unique_constraint("emails_rfq_reference_key", "emails", ["rfq_reference"])

    op.execute("CREATE SEQUENCE rfq_reference_seq START WITH 1")
    op.execute(f"""
        WITH numbered AS ({NUMBERED})
        UPDATE emails
        SET rfq_reference = 'RFQ-' || lpad(numbered.number::text, 4, '0')
        FROM numbered
        WHERE emails.id = numbered.id
    """)
    # Carry on from the last number rather than from 1. An empty database has
    # no row to set it with, and the sequence is already where it should be.
    op.execute("""
        SELECT setval('rfq_reference_seq', counted.issued, true)
        FROM (SELECT count(*)::bigint AS issued FROM emails WHERE rfq_reference IS NOT NULL) counted
        WHERE counted.issued > 0
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SEQUENCE rfq_reference_seq")
    op.drop_constraint("emails_rfq_reference_key", "emails", type_="unique")
    op.drop_column("emails", "rfq_reference")
    op.drop_column("rfq_lines", "offer_unit_price")
