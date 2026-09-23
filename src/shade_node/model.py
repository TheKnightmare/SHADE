from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | str | None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return utc_now().isoformat().replace("+00:00", "Z")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_text(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def token_set(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "at", "is", "are", "was", "were"}
    return {word for word in normalize_text(text).split() if len(word) > 2 and word not in stop}


@dataclass(slots=True)
class Observation:
    source_id: str
    source_name: str
    source_type: str
    source_family: str
    external_id: str
    title: str
    body: str
    url: str
    category: str = "general"
    location: str = ""
    severity: str = "unknown"
    published_at: str = field(default_factory=lambda: iso_utc(None))
    observed_at: str = field(default_factory=lambda: iso_utc(None))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        stable = "|".join(
            [self.source_id, self.external_id, normalize_text(self.title), normalize_text(self.body)]
        )
        return sha256(stable.encode("utf-8")).hexdigest()

    @property
    def event_fingerprint(self) -> str:
        basis = "|".join([self.category, normalize_text(self.location), normalize_text(self.title)])
        return sha256(basis.encode("utf-8")).hexdigest()[:24]

    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(slots=True)
class SourceConfig:
    id: str
    kind: str
    name: str
    url: str
    source_type: str
    source_family: str
    enabled: bool = True
    category: str = "general"
    emcomm_only: bool = False

    pack: str = 'custom'
    area: str = ''
    min_poll_seconds: int = 900
    api_key_env: str = ""
    max_posts_per_poll: int = 100
    trusted_for_relay: bool = False
    sites: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=lambda: ['00060', '00065'])
    threshold: float | None = None
    instance: str = ''
    hashtag: str = ''
    keyword_tier: str = ''
    query_terms: list[str] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict, repr=False)
    park_codes: list[str] = field(default_factory=list)
    region_terms: list[str] = field(default_factory=list)

    def __post_init__(self):
        from urllib.parse import urlsplit
        parsed=urlsplit(self.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Sources require HTTPS URLs without embedded credentials')
        if self.kind == 'mastodon_hashtag' and not self.source_family.strip():
            instance = self.instance or parsed.hostname or 'instance'
            hashtag = (self.hashtag or '').lstrip('#') or 'tag'
            self.source_family = f'mastodon-{instance}-{hashtag}'
        if not self.source_family.strip(): raise ValueError('Every source needs an originating family')
        if self.source_type not in {'official','community','media'}: raise ValueError('Invalid source type')
        if self.min_poll_seconds < 60: raise ValueError('Source polling interval must be at least 60 seconds')
