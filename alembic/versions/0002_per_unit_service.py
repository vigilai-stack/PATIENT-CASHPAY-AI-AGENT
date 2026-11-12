"""per-unit service price and appointment quantity

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    service_cols = {col["name"] for col in insp.get_columns("services")}
    if "price_unit" not in service_cols:
        op.add_column(
            "services",
            sa.Column("price_unit", sa.String(length=20), nullable=False, server_default="visit"),
        )
    appt_cols = {col["name"] for col in insp.get_columns("appointments")}
    if "quantity" not in appt_cols:
        op.add_column(
            "appointments",
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "quantity" in {col["name"] for col in insp.get_columns("appointments")}:
        op.drop_column("appointments", "quantity")
    if "price_unit" in {col["name"] for col in insp.get_columns("services")}:
        op.drop_column("services", "price_unit")
