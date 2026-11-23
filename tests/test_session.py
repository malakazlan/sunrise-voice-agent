from app.config import Settings
from app.db.session import create_database_engine, create_sync_engine

OFFLINE = Settings(
    _env_file=None,
    app_env="dev",
    postgres_host="db.invalid",
    postgres_db="sunrise_offline",
    postgres_user="sunrise_offline",
    postgres_password="offline-test-password",
)


def test_every_engine_hides_parameters() -> None:
    assert create_database_engine(OFFLINE).sync_engine.hide_parameters is True
    assert create_sync_engine(OFFLINE).hide_parameters is True
