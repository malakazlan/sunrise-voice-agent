from alembic import context
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.config import StartupSettings
from app.db.models import Base
from app.db.session import create_owner_sync_engine, startup_wait_failure
from app.guard.logging import configure_logging


def run_migrations_online(settings: StartupSettings) -> None:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                compare_server_default=True,
                autogenerate_plugins=[
                    "alembic.autogenerate.*",
                    "alembic.ext.checkconstraint_byname",
                ],
            )
            with context.begin_transaction():
                context.run_migrations()
    except DBAPIError as error:
        failure = startup_wait_failure("migrations", error)
        if failure is None:
            raise
        raise failure from error
    finally:
        engine.dispose()


settings = StartupSettings()
configure_logging(settings)
run_migrations_online(settings)
