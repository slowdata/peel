"""Bounded RSS catch-up when a source is first enabled."""

from __future__ import annotations

import structlog

from peel.db import DB
from peel.models import Track
from peel.sources.base import Source
from peel.sources.rss import RSSSource

log = structlog.get_logger()
BOOTSTRAP_LOOKBACK_DAYS = 30


def fetch_source(db: DB, source: Source) -> list[Track]:
    """Keep normal polling cheap; bootstrap existing RSS entries over 30 days.

    No implicit pagination, article scraping or state writes. A successful
    source run is the watermark; errors must not consume the bootstrap.
    Articles already absent from the RSS require explicit archive recovery.
    """
    if not isinstance(source, RSSSource) or source.lookback_days is None:
        return source.fetch()
    succeeded = db.conn.execute(
        "SELECT 1 FROM source_runs WHERE source_id = ? AND status = 'ok' LIMIT 1",
        (source.id,),
    ).fetchone()
    if succeeded:
        return source.fetch()
    normal = source.lookback_days
    try:
        source.lookback_days = max(normal, BOOTSTRAP_LOOKBACK_DAYS)
        log.info("source.bootstrap", source_id=source.id, lookback_days=source.lookback_days)
        return source.fetch()
    finally:
        source.lookback_days = normal
