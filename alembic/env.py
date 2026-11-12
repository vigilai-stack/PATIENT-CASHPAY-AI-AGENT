import os

from sqlalchemy import create_engine

import app.models  # noqa: F401  (registers tables)
from alembic import context
from app.db import Base

url = os.getenv("DATABASE_URL", "sqlite:///cashpay.db")


def run():
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=Base.metadata, render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run()
