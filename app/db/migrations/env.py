from logging.config import fileConfig

from alembic import context
from sqlalchemy.pool import NullPool

from app.config import StartupSettings
from app.db.models import Base
from app.db.session import create_owner_sync_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations_online() -> None:
    engine = create_owner_sync_engine(StartupSettings(), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_server_default=True,
            autogenerate_plugins=["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"],
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
