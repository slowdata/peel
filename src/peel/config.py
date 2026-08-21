"""Centralised configuration via pydantic-settings.

Decisão: usar pydantic-settings em vez de os.environ[] directamente.
Razão: validação automática (crasha ao arrancar se faltar um secret, não a meio da run),
type-safety, e suporte para múltiplas fontes (ficheiro .env, variáveis de env,
GitHub Actions Secrets) sem mudar código.

A classe Settings carrega de .env e de variáveis de ambiente. Em produção (GitHub Actions),
não há .env — usa as Secrets injectadas como variáveis de env. Em dev, .env é ignored
pelo git, portanto secrets locais ficam privados.
"""

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Carrega secrets e config do ficheiro .env ou variáveis de ambiente."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    spotify_client_id: str = Field(alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(alias="SPOTIFY_CLIENT_SECRET")
    spotify_refresh_token: str = Field(alias="SPOTIFY_REFRESH_TOKEN")
    peel_playlist_id: str = Field(alias="PEEL_PLAYLIST_ID")
    # Playlist de triagem (candidatas da semana, para ouvir e avaliar). Se vazia,
    # o run mantém o comportamento antigo (escreve na playlist principal).
    peel_review_playlist_id: str = Field(default="", alias="PEEL_REVIEW_PLAYLIST_ID")

    db_path: str = "data/peel.db"
    match_threshold: int = 85
    peel_playlist_window_weeks: int = Field(default=2, alias="PEEL_PLAYLIST_WINDOW_WEEKS")
    # Janela da playlist de TRIAGEM (review): mais larga que a final, para acumular
    # material de várias semanas e dar 20-30 candidatas para avaliar de uma vez.
    peel_review_playlist_window_weeks: int = Field(
        default=4, alias="PEEL_REVIEW_PLAYLIST_WINDOW_WEEKS"
    )
    peel_max_tracks_per_source: int = Field(default=12, alias="PEEL_MAX_TRACKS_PER_SOURCE")
    # Candidatas por run na triagem. A final (peel finalize) corta depois ao Top 7
    # por semana, mas a triagem pode ter múltiplos de 7 para ouvir e avaliar.
    peel_max_tracks_per_run: int = Field(
        default=28,
        ge=1,
        le=28,
        alias="PEEL_MAX_TRACKS_PER_RUN",
    )
    peel_max_source_item_age_days: int = Field(
        default=30,
        alias="PEEL_MAX_SOURCE_ITEM_AGE_DAYS",
    )
    # Fila privada para ouvir e avaliar. Pode ser maior que os sete álbuns
    # publicados, mas fica abaixo de 20 para continuar humanamente manejável.
    peel_max_albums_to_review: int = Field(
        default=11,
        ge=1,
        le=19,
        alias="PEEL_MAX_ALBUMS_TO_REVIEW",
    )
    # Janela (dias) em que tentamos re-matchear tracks que falharam no Spotify.
    # Motivo: blogs frequentemente publicam antes do release global de sexta, ou
    # o track chega ao Spotify dias/semanas depois. 30 dias cobre ambos sem
    # inflacionar a tabela indefinidamente.
    unmatched_retry_days: int = Field(default=30, alias="PEEL_UNMATCHED_RETRY_DAYS")
    # Badge visual no digest quando a afinidade calculada localmente passa o limiar.
    affinity_badge_threshold: float = Field(
        default=0.75,
        alias="PEEL_AFFINITY_BADGE_THRESHOLD",
    )

    @field_validator("peel_max_albums_to_review")
    @classmethod
    def _album_review_limit_must_be_odd(cls, value: int) -> int:
        if value % 2 == 0:
            raise ValueError("PEEL_MAX_ALBUMS_TO_REVIEW must be odd")
        return value

    # Telegram opcional
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    @computed_field
    @property
    def telegram_enabled(self) -> bool:
        """True se ambos token e chat_id estão configurados."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()  # type: ignore[call-arg]
