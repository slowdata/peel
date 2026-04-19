"""Centralised configuration via pydantic-settings.

Decisão: usar pydantic-settings em vez de os.environ[] directamente.
Razão: validação automática (crasha ao arrancar se faltar um secret, não a meio da run),
type-safety, e suporte para múltiplas fontes (ficheiro .env, variáveis de env,
GitHub Actions Secrets) sem mudar código.

A classe Settings carrega de .env e de variáveis de ambiente. Em produção (GitHub Actions),
não há .env — usa as Secrets injectadas como variáveis de env. Em dev, .env é ignored
pelo git, portanto secrets locais ficam privados.
"""

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Carrega secrets e config do ficheiro .env ou variáveis de ambiente."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    spotify_client_id: str = Field(alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(alias="SPOTIFY_CLIENT_SECRET")
    spotify_refresh_token: str = Field(alias="SPOTIFY_REFRESH_TOKEN")
    peel_playlist_id: str = Field(alias="PEEL_PLAYLIST_ID")

    db_path: str = "data/peel.db"
    match_threshold: int = 85
    peel_playlist_window_weeks: int = Field(default=2, alias="PEEL_PLAYLIST_WINDOW_WEEKS")

    # Telegram opcional
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    @computed_field
    @property
    def telegram_enabled(self) -> bool:
        """True se ambos token e chat_id estão configurados."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()  # type: ignore[call-arg]
