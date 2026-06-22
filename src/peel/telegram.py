"""Envia digest semanal via Telegram (HTTP POST puro para api.telegram.org)."""

from __future__ import annotations

from html import escape

import httpx
import structlog

from peel.config import settings
from peel.sources.registry import source_label as _friendly

log = structlog.get_logger()

API_BASE = "https://api.telegram.org"
# Limite hard da API Telegram para o texto de sendMessage (4096 chars).
# Sem chunking, uma semana cheia (14 tracks + 15 álbuns + 7 picks + external)
# pode exceder e falhar com HTTP 400, calado pelo except do send_digest.
MAX_MESSAGE_LENGTH = 4096
DigestItem = (
    tuple[str, str, str, str | None] | tuple[str, str, str, str | None, int]
)  # (source_id, artist, title/album, url[, source_count])
AlbumPickItem = tuple[str, str, int, tuple[str, ...], str | None]
# (artist, album, source_count, sources, preferred_url)


def send_digest(
    new_tracks: list[DigestItem],
    new_albums: list[DigestItem],
    playlist_id: str,
    external_entries: list[DigestItem] | None = None,
    album_recommendations: list[AlbumPickItem] | None = None,
) -> None:
    """Envia digest semanal via Telegram.

    Se token ou chat_id em falta, skip silenciosamente (log info).
    Se HTTP falhar, loga exception mas NÃO levanta (digest é nice-to-have).

    Args:
        new_tracks: Lista de (source_id, artist, title, url) das tracks novas
        new_albums: Lista de (source_id, artist, album, url) dos álbuns novos
        playlist_id: ID da playlist Spotify
        external_entries: Items com link externo que não entraram no Spotify
        album_recommendations: Seleção semanal "7 Álbuns a Ouvir"
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.skipped", reason="credentials_missing")
        return

    text = _format_message(
        new_tracks,
        new_albums,
        playlist_id,
        external_entries or [],
        album_recommendations or [],
    )
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"

    # Chunking: semanas grandes podem exceder 4096 chars. Parte em newlines
    # para manter tags HTML inteiras (cada linha é um elemento completo).
    chunks = _split_message(text, MAX_MESSAGE_LENGTH)
    sent = 0
    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = httpx.post(url, json=payload, timeout=15)
            response.raise_for_status()
            sent += 1
        except Exception as e:
            log.exception(
                "telegram.failed",
                chunk=index,
                total_chunks=len(chunks),
                error=str(e),
            )
            # Digest é best-effort: se um chunk falha, não vale a pena martelar
            # os restantes — o utilizador fica com o que já chegou.
            return

    log.info(
        "telegram.sent",
        tracks=len(new_tracks),
        albums=len(new_albums),
        chunks=sent,
    )


def _split_message(text: str, max_len: int) -> list[str]:
    """Parte o texto em chunks ≤ max_len cortando em newlines.

    Cada linha do digest é um elemento HTML completo (ex.: '• <a …>label</a>'),
    pelo que partir em fronteiras de linha preserva a validade do parse_mode
    HTML. Linhas individuais maiores que max_len (caso patológico) são
    hard-wrapped, mesmo que possam partir uma tag — prefere-se entregar texto a
    falhar calado por tamanho.
    """
    if len(text) <= max_len:
        return [text]

    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        while len(line) > max_len:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:max_len])
            line = line[max_len:]
        sep = 1 if current else 0  # \n que precede a linha no join
        if current_len + sep + len(line) > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            sep = 0
        current.append(line)
        current_len += sep + len(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_message(
    new_tracks: list[DigestItem],
    new_albums: list[DigestItem],
    playlist_id: str,
    external_entries: list[DigestItem] | None = None,
    album_recommendations: list[AlbumPickItem] | None = None,
) -> str:
    """Formata mensagem HTML do Telegram.

    Args:
        new_tracks: Lista de (source_id, artist, title, url)
        new_albums: Lista de (source_id, artist, album, url)
        playlist_id: ID da playlist Spotify
        external_entries: Items com link externo que não entraram no Spotify
        album_recommendations: Seleção semanal "7 Álbuns a Ouvir"

    Returns:
        Mensagem formatada em HTML para Telegram
    """
    lines = ["<b>🎵 Peel — Weekly Digest</b>", ""]

    if new_tracks:
        lines.append(f"<b>Novas tracks ({len(new_tracks)})</b>")
        for item in new_tracks[:20]:
            lines.append(_format_digest_item(item))
        if len(new_tracks) > 20:
            lines.append(f"<i>... e mais {len(new_tracks) - 20}</i>")
        lines.append("")
    else:
        lines.append("<i>Sem tracks novas esta semana.</i>")
        lines.append("")

    if new_albums:
        lines.append(f"<b>💿 Álbuns da semana ({len(new_albums)})</b>")
        for item in new_albums[:15]:
            lines.append(_format_digest_item(item))
    else:
        lines.append("<i>Sem álbuns novos esta semana.</i>")

    album_picks = album_recommendations or []
    if album_picks:
        lines.append("")
        lines.append(f"<b>🎧 7 Álbuns a Ouvir ({len(album_picks)})</b>")
        for item in album_picks[:7]:
            lines.append(_format_album_pick(item))

    external_items = external_entries or []
    if external_items:
        lines.append("")
        lines.append(f"<b>🔗 Escutas externas ({len(external_items)})</b>")
        for item in external_items[:15]:
            lines.append(_format_digest_item(item))
        if len(external_items) > 15:
            lines.append(f"<i>... e mais {len(external_items) - 15}</i>")

    lines.append("")
    lines.append(
        f'<a href="https://open.spotify.com/playlist/{escape(playlist_id)}">🎧 Abrir playlist</a>'
    )

    return "\n".join(lines)


def _format_album_pick(item: AlbumPickItem) -> str:
    artist, album, source_count, sources, url_ = item
    label = f"{escape(artist)} — {escape(album)}"
    consensus = source_count > 1
    prefix = "• ⭐ " if consensus else "• "
    source_label = ", ".join(escape(_friendly(source)) for source in sources)
    if consensus:
        source_label = f"{source_count} fontes: {source_label}"
    source = f" <i>({source_label})</i>" if source_label else ""
    if url_:
        return f'{prefix}<a href="{escape(url_)}">{label}</a>{source}'
    return f"{prefix}{label}{source}"


def _format_digest_item(item: DigestItem) -> str:
    source_id, artist, title, url_, source_count = _unpack_item(item)
    return _format_item(source_id, artist, title, url_, source_count)


def _unpack_item(item: DigestItem) -> tuple[str, str, str, str | None, int]:
    if len(item) == 5:
        source_id, artist, title, url_, source_count = item
        return source_id, artist, title, url_, source_count
    source_id, artist, title, url_ = item
    return source_id, artist, title, url_, 1


def _format_item(
    source_id: str,
    artist: str,
    title: str,
    url_: str | None,
    source_count: int = 1,
) -> str:
    label = f"{escape(artist)} — {escape(title)}"
    consensus = source_count > 1
    prefix = "• ⭐ " if consensus else "• "
    source_label = escape(_friendly(source_id))
    if consensus:
        source_label = f"{source_label}, {source_count} fontes"
    source = f" <i>({source_label})</i>"
    if url_:
        return f'{prefix}<a href="{escape(url_)}">{label}</a>{source}'
    return f"{prefix}{label}{source}"
