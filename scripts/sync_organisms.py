#!/usr/bin/env python3
"""
Fetch paginated GET /api/organisms (same contract as biogenome-portal catalog list),
merge into a JSONL file keyed by taxid: add new rows, replace changed rows in place,
drop taxids no longer returned by the API.

Writes GitHub Actions outputs: changed, added, updated, removed (when GITHUB_OUTPUT is set).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE = "https://dades.biogenoma.cat/api/organisms"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "organisms.jsonl"
PAGE_SIZE = 200


def _canonical(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _taxid_key(row: dict[str, Any]) -> str:
    raw = row.get("taxid")
    if raw is None:
        snippet = repr(row)[:200]
        raise ValueError(f"organism row missing taxid: {snippet}")
    return str(raw)


def _load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {e}") from e
            if not isinstance(row, dict):
                raise SystemExit(f"{path}:{lineno}: expected object, got {type(row)}")
            out[_taxid_key(row)] = row
    return out


def _fetch_page(url: str, offset: int, limit: int) -> dict[str, Any]:
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}limit={limit}&offset={offset}"
    req = urllib.request.Request(
        full,
        headers={
            "Accept": "application/json",
            "User-Agent": "cbp-tracking-sync/1.0 (+https://github.com)",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} for {full}: {e.read()[:500]!r}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Request failed for {full}: {e}") from e

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON from {full}: {e}") from e

    if not isinstance(payload, dict):
        raise SystemExit(f"Expected JSON object from {full}, got {type(payload)}")
    if "total" not in payload or "data" not in payload:
        raise SystemExit(f"Missing total/data in response from {full}")
    if not isinstance(payload["total"], int) or not isinstance(payload["data"], list):
        raise SystemExit(f"Unexpected total/data types from {full}")
    return payload


def fetch_all_organisms(base_url: str) -> dict[str, dict[str, Any]]:
    by_taxid: dict[str, dict[str, Any]] = {}
    offset = 0
    total: int | None = None

    while True:
        page = _fetch_page(base_url, offset, PAGE_SIZE)
        total = page["total"]
        rows = page["data"]
        for row in rows:
            if not isinstance(row, dict):
                raise SystemExit(f"Non-object row in data: {row!r:.200}")
            k = _taxid_key(row)
            by_taxid[k] = row

        offset += len(rows)
        if total is not None and offset >= total:
            break
        if len(rows) == 0:
            break

    if total is not None and len(by_taxid) != total:
        print(
            f"warning: unique taxids={len(by_taxid)} differs from API total={total} "
            "(duplicate taxids in pages?)",
            file=sys.stderr,
        )
    return by_taxid


def _sort_taxids(keys: list[str]) -> list[str]:
    def sort_key(t: str) -> tuple[int, str]:
        try:
            return (0, f"{int(t):020d}")
        except ValueError:
            return (1, t)

    return sorted(keys, key=sort_key)


def write_jsonl(path: Path, by_taxid: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for tid in _sort_taxids(list(by_taxid.keys())):
        row = by_taxid[tid]
        lines.append(
            json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
    text = "".join(lines)
    path.write_text(text, encoding="utf-8")


def _github_output(name: str, value: str) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def main() -> int:
    base = os.environ.get("ORGANISMS_API_URL", DEFAULT_BASE).rstrip("/")
    out_path = Path(os.environ.get("ORGANISMS_JSONL_PATH", str(DEFAULT_OUT))).resolve()

    previous = _load_jsonl(out_path)
    current = fetch_all_organisms(base)

    added = 0
    updated = 0
    for tid, row in current.items():
        if tid not in previous:
            added += 1
        elif _canonical(previous[tid]) != _canonical(row):
            updated += 1

    removed = sum(1 for tid in previous if tid not in current)

    changed = added > 0 or updated > 0 or removed > 0

    if changed:
        write_jsonl(out_path, current)

    _github_output("changed", "true" if changed else "false")
    _github_output("added", str(added))
    _github_output("updated", str(updated))
    _github_output("removed", str(removed))

    print(
        f"organisms sync: total={len(current)} "
        f"added={added} updated={updated} removed={removed} changed={changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
