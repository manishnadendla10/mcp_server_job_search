from .ashby import AshbySource
from .base import Job, JobSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .workday import WorkdaySource

# Registry: companies.yaml `source:` value -> adapter instance.
SOURCES: dict[str, JobSource] = {
    s.name: s for s in (GreenhouseSource(), LeverSource(), AshbySource(), WorkdaySource())
}

__all__ = ["Job", "JobSource", "SOURCES"]
