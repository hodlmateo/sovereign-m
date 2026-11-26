"""Brand and channel configuration. The channel rule lives here, once."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    handle: str
    domains: tuple[str, ...]
    hashtag_max: int
    public_metrics: tuple[str, ...]   # what a visitor can see without logging in
    max_chars: int | None = None


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    area: str                         # folder under the hub: studio or institute
    channels: tuple[Channel, ...]
    kid_safe: bool = False

    def channel(self, key: str) -> Channel | None:
        return next((c for c in self.channels if c.key == key), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.channels)


X = Channel("x", "X", "@MateoVanhorn", ("x.com", "twitter.com"), 0,
            ("views", "likes", "reposts", "replies", "bookmarks"), 280)
TIKTOK = Channel("tiktok", "TikTok", "@vanhornsi", ("tiktok.com",), 2, ("views", "likes", "comments", "shares"))
YOUTUBE = Channel("youtube", "YouTube Shorts", "@vanhorn-si", ("youtube.com", "youtu.be"), 2, ("views", "likes", "comments"))
FACEBOOK = Channel("facebook", "Facebook", "UNVERIFIED", ("facebook.com", "fb.com", "fb.watch"), 3, ("likes", "comments", "shares"))
INSTAGRAM = Channel("instagram", "Instagram", "UNVERIFIED", ("instagram.com",), 3, ("likes", "comments"))
WHATSAPP = Channel("whatsapp", "WhatsApp", "UNVERIFIED", ("whatsapp.com", "wa.me"), 3, ())

VANHORN = Brand("vanhorn-si", "Vanhorn SI", "studio", (X, TIKTOK, YOUTUBE))
INSTITUTE = Brand("institute", "Institute", "institute", (FACEBOOK, INSTAGRAM, WHATSAPP), kid_safe=True)
BRANDS = {b.key: b for b in (VANHORN, INSTITUTE)}


def hub_root() -> Path:
    return Path(os.environ.get("M_HUB", "") or Path.home() / "m_hub")


class Paths:
    def __init__(self, brand: Brand, hub: Path | None = None) -> None:
        self.brand = brand
        self.hub = hub or hub_root()
        self.area = self.hub / brand.area
        self.brand_dir = self.area / "brand"
        self.drafts = self.area / "drafts"
        self.ideas = self.area / "ideas"
        self.results = self.area / "results"
        self.published = self.area / "published.md"
        self.mentions = self.area / "mentions"
