from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Takeback"
    database_url: str = "sqlite:///./data/db/takeback.db"
    data_dir: str = "./data"
    debug: bool = False
    # The language the UI is served in when the request asks for none
    # (app/i18n.py). A browser's own preference is honoured ahead of it.
    lang: str = "fr"

    # Budgets on untrusted archives (app/limits.py). An export split across
    # several zips is one import, so the per-file cap is not enough on its own:
    # the batch cap is what actually bounds a single import. The extraction cap
    # is the one that stops a zip bomb.
    max_upload_mb: int = 2048
    max_batch_upload_mb: int = 8192
    max_extract_gb: float = 20.0

    # InfluxDB 1.x sink (app/sinks/influxdb.py, what goes out: app/influx_export.py):
    # off unless the server URL is set.
    influxdb_url: Optional[str] = None
    # The `user` tag of every point, as in a shared database.
    influxdb_user: Optional[str] = None
    # local_hour / local_weekday fields: InfluxQL can't shift a time zone itself.
    influxdb_tz: str = "Europe/Paris"
    # Daily full push, at this hour (UTC).
    influxdb_push_hour: int = 3
    influxdb_create_databases: bool = True

    class Config:
        env_file = ".env"
        env_prefix = "TAKEBACK_"


settings = Settings()
