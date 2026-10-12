import os

from alembic import context
from sqlalchemy import create_engine

# Migrations are hand-written SQLAlchemy Core; there is no ORM metadata to autogenerate from.
engine = create_engine(os.environ["DATABASE_URL"])

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
