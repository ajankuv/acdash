"""Cloud history (`log/dataPage`) → chart-friendly series.

See AC_INFINITY_FIELDS.md — ``time`` is the newer bound, ``endTime`` the older bound (unix seconds).

We run **two** paginated passes with ``orderDirection=1`` and ``orderDirection=0`` and **merge** rows.
The APK only uses ``1``, but the cloud sometimes appears to page differently by sort order; merging
recovers a wider time span when the two passes return complementary slices.

**We do not narrow ``endTime`` using ``validFrom``** (see ``RND/HISTORY_DATA_PAGE.md``).

Long ranges are stepped in **multi-day calendar segments** per pass (not one giant ``endTime``),
then merged. **Too-short segments (e.g. 16 h) break pagination:** the first page’s oldest
``createTime`` often falls **before** ``window_lo``, so ``_paginate_window`` exits immediately
(``edge_sec <= window_lo``) and you only ever load ~one page per band — yielding ~17 h of span
even when ``total`` is tens of thousands. **168 h (7 d)** bands match field behavior; see
``RND/HISTORY_DATA_PAGE.md`` (coarse scan).

The official app can scroll years because it keeps a **local** database; we only have HTTP.

Deduplication uses **raw** ``createTime`` integers so samples in the same second are not collapsed.
"""

from __future__ import annotations

from typing import Any

# Calendar width for each ``(endTime, time]`` band when ``hours`` exceeds the threshold below.
# 7-day steps keep ``window_lo`` old enough that paging usually survives the first batch.
CHART_SEGMENT_HOURS = 168.0
CHART_SEGMENT_THRESHOLD_HOURS = 24.0


def _div100(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return None


def normalize_create_time_sec(ct: Any) -> int | None:
    """Parse ``createTime`` / ``validFrom`` to unix seconds (handles ms in JSON)."""
    if ct is None:
        return None
    try:
        t = int(ct)
    except (TypeError, ValueError):
        try:
            t = int(float(ct))
        except (TypeError, ValueError):
            return None
    if t > 10_000_000_000:
        t //= 1000
    return t


def _raw_create_key(ct: Any) -> int | None:
    """Stable dict key from API ``createTime`` (seconds or ms as returned)."""
    if ct is None:
        return None
    try:
        return int(ct)
    except (TypeError, ValueError):
        try:
            return int(float(ct))
        except (TypeError, ValueError):
            return None


def _batch_oldest_sec(batch: list[dict[str, Any]]) -> int | None:
    best: int | None = None
    for r in batch:
        t = normalize_create_time_sec(r.get("createTime"))
        if t is None:
            continue
        if best is None or t < best:
            best = t
    return best


def history_row_to_point(row: dict[str, Any]) -> dict[str, Any] | None:
    """One NetHistory-shaped row → numeric fields for charts (scaled where typical)."""
    t_sec = normalize_create_time_sec(row.get("createTime"))
    if t_sec is None:
        return None
    temp_c = _div100(row.get("temperature"))
    rh = _div100(row.get("humidity"))
    vpd = _div100(row.get("vpdNums"))
    fan = row.get("allSpead")
    port_fan = row.get("portSpead")
    try:
        fan_i = int(fan) if fan is not None else None
    except (TypeError, ValueError):
        fan_i = None
    try:
        port_fan_i = int(port_fan) if port_fan is not None else None
    except (TypeError, ValueError):
        port_fan_i = None
    return {
        "t": t_sec,
        "t_ms": t_sec * 1000,
        "temp_c": temp_c,
        "rh": rh,
        "vpd_kpa": vpd,
        "fan": fan_i,
        "port_fan": port_fan_i,
    }


def thin_points(points: list[dict[str, Any]], max_points: int = 1200) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    return points[::step]


def _paginate_window(
    history_page_fn: Any,
    dev_id: str,
    window_end: int,
    window_start: int,
    page_size: int,
    max_pages: int,
    pause_sec: float,
    *,
    order_direction: int = 1,
    stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Walk backward within ``(window_start, window_end]`` (unix seconds)."""
    import time as time_mod

    rows: list[dict[str, Any]] = []
    cur_end = window_end
    window_lo = window_start
    valid_from_meta: int | None = None
    pages = 0
    prev_edge: int | None = None

    while cur_end > window_lo and pages < max_pages:
        data = history_page_fn(
            dev_id,
            cur_end,
            window_lo,
            page_size,
            order_direction=order_direction,
        )
        pages += 1
        if not isinstance(data, dict):
            break

        if stats is not None:
            t = data.get("total")
            if isinstance(t, int) and t > stats.get("api_total_max", 0):
                stats["api_total_max"] = t

        vf = data.get("validFrom")
        if vf is not None:
            vi = normalize_create_time_sec(vf)
            if vi is not None and vi > 0 and valid_from_meta is None:
                valid_from_meta = vi

        batch = data.get("rows") or []
        if not batch:
            break

        rows.extend(batch)

        edge_sec = _batch_oldest_sec(batch)
        if edge_sec is None:
            break
        if edge_sec <= window_lo:
            break

        next_end = edge_sec - 1
        if prev_edge is not None and edge_sec >= prev_edge:
            cur_end = max(window_lo + 1, cur_end - 21600)
            prev_edge = None
            time_mod.sleep(pause_sec)
            continue
        if next_end >= cur_end:
            cur_end = max(window_lo + 1, cur_end - 21600)
            prev_edge = None
            time_mod.sleep(pause_sec)
            continue

        prev_edge = edge_sec
        cur_end = next_end
        time_mod.sleep(pause_sec)

    return rows, pages, valid_from_meta


def _gather_one_order_direction(
    history_page_fn: Any,
    dev_id: str,
    hours: float,
    time_end: int,
    time_start: int,
    page_size: int,
    max_pages: int,
    pause_sec: float,
    order_direction: int,
    stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int | None, int]:
    """Segment + paginate for one ``orderDirection`` (see module constants)."""
    import time as time_mod

    rows_all: list[dict[str, Any]] = []
    total_pages = 0
    valid_from: int | None = None
    fetch_chunks = 1

    use_segments = hours > CHART_SEGMENT_THRESHOLD_HOURS
    chunk_sec = int(CHART_SEGMENT_HOURS * 3600)

    if use_segments:
        fetch_chunks = 0
        seg_hi = time_end
        while seg_hi > time_start and total_pages < max_pages:
            remain = max_pages - total_pages
            if remain <= 0:
                break
            seg_lo = max(time_start, seg_hi - chunk_sec)
            fetch_chunks += 1
            chunk_rows, chunk_pages, vf = _paginate_window(
                history_page_fn,
                dev_id,
                seg_hi,
                seg_lo,
                page_size,
                remain,
                pause_sec,
                order_direction=order_direction,
                stats=stats,
            )
            total_pages += chunk_pages
            rows_all.extend(chunk_rows)
            if vf is not None and valid_from is None:
                valid_from = vf
            seg_hi = seg_lo
            if seg_hi <= time_start:
                break
            time_mod.sleep(pause_sec)
    else:
        rows_all, total_pages, valid_from = _paginate_window(
            history_page_fn,
            dev_id,
            time_end,
            time_start,
            page_size,
            max_pages,
            pause_sec,
            order_direction=order_direction,
            stats=stats,
        )

    return rows_all, total_pages, valid_from, fetch_chunks


def fetch_history_for_chart(
    *,
    history_page_fn: Any,
    dev_id: str,
    hours: float,
    page_size: int = 1000,
    max_pages: int = 0,
    pause_sec: float = 0.12,
    order_directions: tuple[int, ...] = (1, 0),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Walk pages backward like the Android client.

    ``history_page_fn`` is called as::

        (dev_id, time_end, time_start, page_size, *, order_direction=1) -> data dict

    and must return the raw ``data`` object from ``log/dataPage`` (not full envelope).
    """
    import time

    now = int(time.time())
    time_end = now + 59
    time_start = now - int(max(0.25, hours) * 3600)
    if max_pages <= 0:
        h = max(hours, 0.25)
        # Dense accounts: ~40k+ rows / 30d → allow enough pages per direction (1k page size).
        max_pages = max(200, min(4000, int(h * 28) + 200))

    n_dir = max(1, len(order_directions))
    # Full budget per pass — two passes can mean ~2x API volume for long ranges.
    per_dir_budget = max_pages
    stats: dict[str, Any] = {"api_total_max": 0}

    merged_rows: list[dict[str, Any]] = []
    total_pages = 0
    valid_from: int | None = None
    fetch_chunks_max = 1

    for od in order_directions:
        part, pages, vf, chunks = _gather_one_order_direction(
            history_page_fn,
            dev_id,
            hours,
            time_end,
            time_start,
            page_size,
            per_dir_budget,
            pause_sec,
            od,
            stats,
        )
        merged_rows.extend(part)
        total_pages += pages
        fetch_chunks_max = max(fetch_chunks_max, chunks)
        if vf is not None and valid_from is None:
            valid_from = vf

    by_key: dict[int, dict[str, Any]] = {}
    for r in merged_rows:
        k = _raw_create_key(r.get("createTime"))
        if k is None:
            continue
        by_key[k] = r
    rows_sorted = [by_key[k] for k in sorted(by_key.keys())]

    points: list[dict[str, Any]] = []
    for r in rows_sorted:
        p = history_row_to_point(r)
        if p:
            points.append(p)
    thinned = thin_points(points)
    span_sec = 0
    if len(points) >= 2:
        span_sec = max(0, (points[-1]["t"] - points[0]["t"]))
    window_sec = max(0, time_end - time_start)
    meta = {
        "raw_rows": len(rows_sorted),
        "points": len(thinned),
        "points_unthinned": len(points),
        "hours_requested": hours,
        "span_hours_rounded": round(span_sec / 3600.0, 2) if span_sec else 0,
        "window_hours_rounded": round(window_sec / 3600.0, 2),
        "time_start": time_start,
        "time_end": time_end,
        "valid_from": valid_from,
        "pages_fetched": total_pages,
        "page_size": page_size,
        "fetch_chunks": fetch_chunks_max,
        "order_passes": n_dir,
        "order_directions": list(order_directions),
        "api_total_max": stats.get("api_total_max", 0),
        "chunk_hours": round(CHART_SEGMENT_HOURS, 2)
        if hours > CHART_SEGMENT_THRESHOLD_HOURS
        else None,
    }
    return thinned, meta

