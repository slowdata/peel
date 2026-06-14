"""Envia digest semanal via Telegram (HTTP POST puro para api.telegram.org)."""

from __future__ import annotations

from html import escape

import httpx
import structlog

from peel.config import settings

log = structlog.get_logger()

API_BASE = "https://api.telegram.org"
DigestItem = (
    tuple[str, str, str, str | None] | tuple[str, str, str, str | None, int]
)  # (source_id, artist, title/album, url[, source_count])


def send_digest(
    new_tracks: list[DigestItem],
    new_albums: list[DigestItem],
    playlist_id: str,
    external_entries: list[DigestItem] | None = None,
) -> None:
    """Envia digest semanal via Telegram.

    Se token ou chat_id em falta, skip silenciosamente (log info).
    Se HTTP falhar, loga exception mas NÃO levanta (digest é nice-to-have).

    Args:
        new_tracks: Lista de (source_id, artist, title, url) das tracks novas
        new_albums: Lista de (source_id, artist, album, url) dos álbuns novos
        playlist_id: ID da playlist Spotify
        external_entries: Items com link externo que não entraram no Spotify
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.skipped", reason="credentials_missing")
        return

    text = _format_message(new_tracks, new_albums, playlist_id, external_entries or [])
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = httpx.post(url, json=payload, timeout=15)
        response.raise_for_status()
        log.info("telegram.sent", tracks=len(new_tracks), albums=len(new_albums))
    except Exception as e:
        log.exception("telegram.failed", error=str(e))


def _format_message(
    new_tracks: list[DigestItem],
    new_albums: list[DigestItem],
    playlist_id: str,
    external_entries: list[DigestItem] | None = None,
) -> str:
    """Formata mensagem HTML do Telegram.

    Args:
        new_tracks: Lista de (source_id, artist, title, url)
        new_albums: Lista de (source_id, artist, album, url)
        playlist_id: ID da playlist Spotify
        external_entries: Items com link externo que não entraram no Spotify

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
    source_label = escape(source_id)
    if consensus:
        source_label = f"{source_label}, {source_count} fontes"
    source = f" <i>({source_label})</i>"
    if url_:
        return f'{prefix}<a href="{escape(url_)}">{label}</a>{source}'
    return f"{prefix}{label}{source}"
