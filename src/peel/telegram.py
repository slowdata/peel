"""Envia digest semanal via Telegram (HTTP POST puro para api.telegram.org)."""

from __future__ import annotations

from html import escape

import httpx
import structlog

from peel.config import settings

log = structlog.get_logger()

API_BASE = "https://api.telegram.org"


def send_digest(
    new_tracks: list[tuple[str, str, str | None]],  # (artist, title, url)
    new_albums: list[tuple[str, str, str | None]],  # (artist, album, url)
    playlist_id: str,
) -> None:
    """Envia digest semanal via Telegram.

    Se token ou chat_id em falta, skip silenciosamente (log info).
    Se HTTP falhar, loga exception mas NÃO levanta (digest é nice-to-have).

    Args:
        new_tracks: Lista de (artist, title, url) das tracks novas
        new_albums: Lista de (artist, album, url) dos álbuns novos
        playlist_id: ID da playlist Spotify
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.skipped", reason="credentials_missing")
        return

    text = _format_message(new_tracks, new_albums, playlist_id)
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
    new_tracks: list[tuple[str, str, str | None]],
    new_albums: list[tuple[str, str, str | None]],
    playlist_id: str,
) -> str:
    """Formata mensagem HTML do Telegram.

    Args:
        new_tracks: Lista de (artist, title, url)
        new_albums: Lista de (artist, album, url)
        playlist_id: ID da playlist Spotify

    Returns:
        Mensagem formatada em HTML para Telegram
    """
    lines = ["<b>🎵 Peel — Weekly Digest</b>", ""]

    if new_tracks:
        lines.append(f"<b>Novas tracks ({len(new_tracks)})</b>")
        for artist, title, _ in new_tracks[:20]:
            lines.append(f"• {escape(artist)} — {escape(title)}")
        if len(new_tracks) > 20:
            lines.append(f"<i>... e mais {len(new_tracks) - 20}</i>")
        lines.append("")
    else:
        lines.append("<i>Sem tracks novas esta semana.</i>")
        lines.append("")

    if new_albums:
        lines.append(f"<b>💿 Álbuns da semana ({len(new_albums)})</b>")
        for artist, album, url_ in new_albums[:15]:
            if url_:
                lines.append(f'• <a href="{escape(url_)}">{escape(artist)} — {escape(album)}</a>')
            else:
                lines.append(f"• {escape(artist)} — {escape(album)}")
    else:
        lines.append("<i>Sem álbuns novos esta semana.</i>")

    lines.append("")
    lines.append(
        f'<a href="https://open.spotify.com/playlist/{escape(playlist_id)}">🎧 Abrir playlist</a>'
    )

    return "\n".join(lines)
