"""Parallel post_get helper for Watch front snapshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Sequence

from .client import ApiError, Client


def _fetch_thread(client: Client, post_id: int) -> Optional[Dict]:
    try:
        return client.post_get(post_id)
    except ApiError:
        return None


def fetch_threads(
    client: Client,
    post_ids: Sequence[int],
    *,
    max_workers: int = 12,
) -> Dict[int, Dict]:
    threads: Dict[int, Dict] = {}
    if not post_ids:
        return threads
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_thread, client, pid): pid for pid in post_ids}
        for fut in as_completed(futs):
            data = fut.result()
            if not data or not data.get("post"):
                continue
            threads[int(data["post"]["id"])] = data
    return threads
