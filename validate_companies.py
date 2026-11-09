"""Check every board in companies.yaml against its live API.

Usage:  python validate_companies.py
Prints OK / FAIL per company with the open-job count, so you can spot slugs
that are wrong or companies that switched ATS.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import yaml

from sources import SOURCES

CONFIG = Path(__file__).parent / "companies.yaml"


async def check(client, sem, entry):
    name, source, board = entry["name"], entry["source"], entry["board"]
    adapter = SOURCES.get(source)
    if adapter is None:
        return name, source, board, None, f"no adapter for source '{source}'"
    async with sem:
        try:
            jobs = await adapter.fetch(client, board, name, options=entry)
            return name, source, board, len(jobs), None
        except httpx.HTTPStatusError as e:
            return name, source, board, None, f"HTTP {e.response.status_code}"
        except Exception as e:  # noqa: BLE001 — report anything
            return name, source, board, None, type(e).__name__


async def main() -> int:
    entries = yaml.safe_load(CONFIG.read_text())["companies"]
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(*(check(client, sem, e) for e in entries))
    bad = 0
    for name, source, board, count, err in sorted(results, key=lambda r: r[0].lower()):
        if err:
            bad += 1
            print(f"FAIL  {name:<20} {source}:{board:<18} {err}")
        else:
            print(f"OK    {name:<20} {source}:{board:<18} {count} jobs")
    print(f"\n{len(results) - bad}/{len(results)} boards OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
