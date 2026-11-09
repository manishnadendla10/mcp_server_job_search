"""Common Job model and the adapter interface every job-board source implements.

Adding a new ATS (applicant tracking system) = one new subclass of JobSource.
The rest of the app only ever sees `Job` objects, never raw API payloads.
"""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx


@dataclass
class Job:
    id: str                      # composite: "<source>:<board>:<native id>"
    source: str                  # "greenhouse" | "lever" | "ashby"
    board: str                   # board slug from companies.yaml, e.g. "stripe"
    company: str                 # display name, e.g. "Stripe"
    title: str
    location: str
    url: str
    remote: bool = False
    department: str | None = None
    posted_at: datetime | None = None
    description: str | None = None   # plain text (HTML stripped)
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        """Compact form for search results — keep tool output small."""
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote,
            "posted": self.posted_at.date().isoformat() if self.posted_at else None,
            "url": self.url,
        }


class JobSource(ABC):
    """Adapter interface. One subclass per job-board provider."""

    name: str  # must match the `source` value used in companies.yaml

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, board: str, company: str,
                    options: dict | None = None) -> list[Job]:
        """Return every open job on the given board.

        `options` is the company's full companies.yaml entry, for adapters that
        need extra settings (e.g. Workday's target locations).
        """

    async def fetch_description(self, client: httpx.AsyncClient, job: dict) -> str | None:
        """Override when listings lack descriptions (Workday). `job` is a jobs-table row."""
        return None

    def make_id(self, board: str, native_id: object) -> str:
        return f"{self.name}:{board}:{native_id}"


# ---------- shared helpers ----------

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|h[1-6]|ul|ol|tr)[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\xa0]+")
_NL_RE = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None) -> str:
    """Turn (possibly entity-escaped) HTML into readable plain text."""
    if not raw:
        return ""
    s = html.unescape(raw)          # Greenhouse double-escapes: &lt;p&gt; -> <p>
    s = _BLOCK_RE.sub("\n", s)       # keep paragraph structure
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)            # entities that were inside the tags' text
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def looks_remote(*texts: str | None) -> bool:
    return any(t and "remote" in t.lower() for t in texts)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
