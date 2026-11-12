"""saved patient conversation transcript

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "conversations" not in tables:
        op.create_table(
            "conversations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("patient_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_conversations_patient_id", "conversations", ["patient_id"])
    if "conversation_messages" not in tables:
        op.create_table(
            "conversation_messages",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("speaker", sa.String(length=20), nullable=False),
            sa.Column("patient_id", sa.Integer(), nullable=True),
            sa.Column("staff_id", sa.Integer(), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("client_key", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("reply_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
            sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
            sa.ForeignKeyConstraint(["staff_id"], ["staff_users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("conversation_id", "seq", name="uq_conversation_message_seq"),
            sa.UniqueConstraint("conversation_id", "client_key", name="uq_conversation_client_key"),
        )
        op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    if "conversation_handoffs" not in tables:
        op.create_table(
            "conversation_handoffs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("patient_id", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
            sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("conversation_id"),
        )
        op.create_index("ix_conversation_handoffs_patient_id", "conversation_handoffs", ["patient_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "conversation_handoffs" in tables:
        op.drop_index("ix_conversation_handoffs_patient_id", table_name="conversation_handoffs")
        op.drop_table("conversation_handoffs")
    if "conversation_messages" in tables:
        op.drop_index("ix_conversation_messages_conversation_id", table_name="conversation_messages")
        op.drop_table("conversation_messages")
    if "conversations" in tables:
        op.drop_index("ix_conversations_patient_id", table_name="conversations")
        op.drop_table("conversations")
