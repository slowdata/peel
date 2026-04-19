"""Modelos de domínio centrais.

Decisão: usar pydantic.BaseModel para tudo, mesmo coisas "simples".
Razão: validação automática (ex.: artist/title não podem ser strings vazias),
serialização JSON grátis, e docs/schema automático. Quando o projeto cresce,
são adições simples (regex, transformações, etc.).

Track é imutável (frozen=True) porque, uma vez criada por uma Source, não deve mudar
durante o pipeline. Imutabilidade previne bugs silent onde alguém acidentalmente
modifica um Track à passagem.
"""

from datetime import datetime

from pydantic import BaseModel, field_validator


class Track(BaseModel):
    """Faixa candidata vinda de uma fonte de curadoria.

    Ainda não está emparelhada com o Spotify — isso é tarefa do matcher.
    Imutável após criação para evitar modificações acidentais no pipeline.
    """

    model_config = {"frozen": True}

    source_id: str
    """Identificador estável da fonte, ex.: 'pitchfork_bnt' ou 'bbc6music_recommends'."""

    artist: str
    """Nome do artista. Validado por @field_validator — rejeita strings em branco,
    devolve versão .strip()ped."""

    title: str
    """Título da faixa. Validado por @field_validator — rejeita strings em branco,
    devolve versão .strip()ped."""

    source_url: str | None = None
    """URL opcionalmente útil para logging (link para a review, episódio, etc.)."""

    published_at: datetime | None = None
    """Quando a fonte a publicou. Opcional — nem todas as fontes têm timestamp."""

    raw_title: str | None = None
    """Título original antes de split artist/title, em caso de parsing complexo.
    Útil para debugging quando o split foi mal feito."""

    @field_validator("artist", "title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """Rejeita strings em branco (após .strip()). Devolve versão stripped.

        Isto garante que artist e title nunca são strings vazias ou apenas whitespace.
        ValidationError é levantado ao tentar criar Track(artist="  ", ...).
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("artist and title must not be blank or whitespace-only")
        return stripped
