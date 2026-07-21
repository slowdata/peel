"""Envia digest semanal via Telegram (HTTP POST puro para api.telegram.org)."""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

import httpx
import structlog

from peel.config import settings
from peel.models import ReviewQueueItem
from peel.sources.registry import source_label as _friendly

log = structlog.get_logger()

API_BASE = "https://api.telegram.org"
# Limite hard da API Telegram para o texto de sendMessage (4096 chars).
# Sem chunking, uma semana cheia (14 tracks + 15 álbuns + 7 picks + external)
# pode exceder e falhar com HTTP 400, calado pelo except do send_digest.
MAX_MESSAGE_LENGTH = 4096


# Alias de compatibilidade para o formatter e os seus testes.
TriageItem = ReviewQueueItem

DigestItem = (
    tuple[str, str, str, str | None]
    | tuple[str, str, str, str | None, int]
    | tuple[str, str, str, str | None, int, float]
)  # (source_id, artist, title/album, url[, source_count[, affinity_score]])
AlbumPickItem = (
    tuple[str, str, int, tuple[str, ...], str | None]
    | tuple[str, str, int, tuple[str, ...], str | None, str | None]
)
AlbumPickParts = tuple[str, str, int, tuple[str, ...], str | None, str | None]
# (artist, album, source_count, sources, listen_url[, source_url])


def send_digest(
    new_tracks: list[DigestItem | TriageItem],
    new_albums: list[DigestItem],
    playlist_id: str,
    external_entries: list[DigestItem] | None = None,
    album_recommendations: list[AlbumPickItem] | None = None,
) -> None:
    """Envia digest semanal via Telegram.

    Se token ou chat_id em falta, skip silenciosamente (log info).
    Se HTTP falhar, loga exception mas NÃO levanta (digest é nice-to-have).

    Args:
        new_tracks: tracks novas (legado) ou a lista efectiva da triagem
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
    new_tracks: list[DigestItem | TriageItem],
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

    triage_tracks = [item for item in new_tracks if isinstance(item, TriageItem)]
    if triage_tracks:
        new_count = sum(item.is_new for item in triage_tracks)
        pending_count = len(triage_tracks) - new_count
        lines.append(f"<b>🎧 Triagem actual ({len(triage_tracks)})</b>")
        lines.append(f"<i>🆕 {new_count} novas · ↻ {pending_count} pendentes</i>")
        for item in triage_tracks:
            lines.append(_format_triage_item(item))
        lines.append("")
    elif new_tracks:
        lines.append(f"<b>Novas tracks ({len(new_tracks)})</b>")
        for item in new_tracks[:20]:
            assert not isinstance(item, TriageItem)
            lines.append(_format_digest_item(item))
        if len(new_tracks) > 20:
            lines.append(f"<i>... e mais {len(new_tracks) - 20}</i>")
        lines.append("")
    else:
        lines.append("<i>Sem tracks novas esta semana.</i>")
        lines.append("")

    album_picks = album_recommendations or []
    # A queue canónica é a entrega; raw mentions ficam no relatório/DB para não
    # duplicar discos no Telegram com uma ordem ou links diferentes.
    if new_albums and not album_picks:
        lines.append(f"<b>💿 Álbuns da semana ({len(new_albums)})</b>")
        for item in new_albums[:15]:
            lines.append(_format_digest_item(item))
    elif not album_picks:
        lines.append("<i>Sem álbuns novos esta semana.</i>")

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
    artist, album, source_count, sources, url_, source_url = _unpack_album_pick(item)
    label = f"{escape(artist)} — {escape(album)}"
    consensus = source_count > 1
    prefix = "• ⭐ " if consensus else "• "
    source_label = ", ".join(escape(_friendly(source)) for source in sources)
    if consensus:
        source_label = f"{source_count} fontes: {source_label}"
    source = f" <i>({source_label})</i>" if source_label else ""
    source_link = _format_source_link(source_url, url_)
    if url_:
        return f'{prefix}<a href="{escape(url_)}">{label}</a>{source}{source_link}'
    return f"{prefix}{label}{source}{source_link}"


def _unpack_album_pick(item: AlbumPickItem) -> AlbumPickParts:
    if len(item) == 6:
        artist, album, source_count, sources, url_, source_url = item
        return artist, album, source_count, sources, url_, source_url
    artist, album, source_count, sources, url_ = item
    return artist, album, source_count, sources, url_, None


def _format_source_link(source_url: str | None, primary_url: str | None) -> str:
    if not source_url or source_url == primary_url:
        return ""
    return f' · <a href="{escape(source_url)}">{escape(_source_link_label(source_url))}</a>'


def _source_link_label(url_: str) -> str:
    try:
        host = urlparse(url_).hostname or ""
    except ValueError:
        return "Fonte"
    host = host.removeprefix("www.").lower()
    if host.endswith("bandcamp.com"):
        return "Bandcamp"
    if host.endswith("open.spotify.com"):
        return "Spotify"
    return "Review"


def _format_digest_item(item: DigestItem) -> str:
    source_id, artist, title, url_, source_count, affinity = _unpack_item(item)
    return _format_item(source_id, artist, title, url_, source_count, affinity)


def _unpack_item(item: DigestItem) -> tuple[str, str, str, str | None, int, float | None]:
    if len(item) == 6:
        source_id, artist, title, url_, source_count, affinity = item
        return source_id, artist, title, url_, source_count, affinity
    if len(item) == 5:
        source_id, artist, title, url_, source_count = item
        return source_id, artist, title, url_, source_count, None
    source_id, artist, title, url_ = item
    return source_id, artist, title, url_, 1, None


def _format_triage_item(item: TriageItem) -> str:
    label = f"{escape(item.artist)} — {escape(item.title)}"
    status = (
        f"🆕 nova {escape(item.current_week)}"
        if item.is_new
        else f"↻ pendente {escape(item.added_at_week)}"
    )
    badges = []
    if item.source_count > 1:
        badges.append("⭐")
    if item.affinity >= settings.affinity_badge_threshold:
        badges.append("🎯")
    badge_text = " ".join(badges)
    prefix = f"• {status}" + (f" {badge_text}" if badge_text else "")
    source_label = escape(_friendly(item.source_id))
    if item.source_count > 1:
        source_label = f"{source_label}, {item.source_count} fontes"
    source = f" <i>({source_label})</i>"
    track_url = item.spotify_uri.replace("spotify:track:", "https://open.spotify.com/track/")
    review = _format_source_link(item.source_url, track_url)
    return f'{prefix} <a href="{escape(track_url)}">{label}</a>{source}{review}'


def _format_item(
    source_id: str,
    artist: str,
    title: str,
    url_: str | None,
    source_count: int = 1,
    affinity: float | None = None,
) -> str:
    label = f"{escape(artist)} — {escape(title)}"
    consensus = source_count > 1
    badges = []
    if consensus:
        badges.append("⭐")
    if affinity is not None and affinity >= settings.affinity_badge_threshold:
        badges.append("🎯")
    prefix = "• " + (" ".join(badges) + " " if badges else "")
    source_label = escape(_friendly(source_id))
    if consensus:
        source_label = f"{source_label}, {source_count} fontes"
    source = f" <i>({source_label})</i>"
    if url_:
        return f'{prefix}<a href="{escape(url_)}">{label}</a>{source}'
    return f"{prefix}{label}{source}"
