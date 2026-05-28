# -*- coding: utf-8 -*-
"""
Filtering helpers for LidarBC features.

Filters operate on tile dicts that include a 'year' key sourced from the
server's `year` attribute (an integer). For files with no year on record,
we keep them regardless of filter mode so we never silently drop something
we can't categorize.

Note: in earlier versions of this plugin, we parsed dates out of filenames
(handling YYYYMMDD_YYYYMMDD pairs and year-only variants). That logic has
been removed in favor of the cleaner authoritative `year` attribute.
"""

import re
from collections import defaultdict


def _coerce_year(value):
    """Return an int year, or None if value isn't a usable year.

    Accepts ints, numeric strings, or strings starting with a 4-digit year
    (e.g. "2024", "2024-07-26"). 1900–2100 only.
    """
    if value is None:
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, float):
        year = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            year = int(s[:4])
        except ValueError:
            return None
    else:
        return None
    if 1900 <= year <= 2100:
        return year
    return None


def filename_from_url(url):
    """Return the trailing filename component of a URL."""
    if not url:
        return ""
    base = url.split("?", 1)[0].split("#", 1)[0]
    return base.rstrip("/").rsplit("/", 1)[-1]


def _tile_grouping_key(tile):
    """Key used to group different versions of the same physical tile together.

    Preferred source: the 'BCGS Tile Name' attribute from the server (e.g.
    "092b063" or "092b023_3_4_2"). When two server features share the same
    BCGS Tile Name, they represent the same physical tile area — possibly
    from different acquisition years — and the version filter should treat
    them as competitors.

    Fallback when BCGS Tile Name is missing/empty for a particular row:
    strip the date out of the filename and compare what's left. This is
    less reliable (filename conventions vary over time, e.g. "xli1m" vs
    "xl1m" for the same tile), but it's better than treating every file
    as its own group.

    Last resort (no filename either): use OBJECTID, which never groups
    anything but at least keeps the tile in the result.
    """
    # Prefer the authoritative attribute when present
    bcgs = tile.get("bcgs_tile_name")
    if bcgs:
        bcgs_str = str(bcgs).strip()
        if bcgs_str:
            return ("__bcgs__", bcgs_str.lower())

    # Fallback: filename with date stripped
    fname = tile.get("filename") or filename_from_url(tile.get("url", ""))
    if not fname:
        return ("__oid__", tile.get("object_id"))
    # Strip extension
    stem = fname.rsplit(".", 1)[0]
    # Remove any embedded date-like tokens (YYYYMMDD or 4-digit year between
    # underscores).
    stem = re.sub(r"_\d{8}", "_", stem)
    stem = re.sub(r"_\d{4}(?=_|$)", "_", stem)
    while "__" in stem:
        stem = stem.replace("__", "_")
    return ("__key__", stem.rstrip("_"))


def filter_versions(tiles, mode):
    """Group tiles by tile identity and pick one per group per mode.

    :param tiles: iterable of dicts with at least 'url' (or 'filename') and
                  optionally 'year'.
    :param mode: 'latest', 'oldest', or 'all'.
    :return: list of dicts (subset of input).

    Files with no usable 'year' value are always kept regardless of mode
    so we never silently drop tiles. Tiles missing a year sit alongside
    the chosen winner from each group.
    """
    if mode == "all":
        return list(tiles)
    if mode not in ("latest", "oldest"):
        raise ValueError(f"Unknown version mode: {mode!r}")

    groups = defaultdict(list)
    no_year = []
    for tile in tiles:
        # Cache filename so downstream code doesn't have to recompute
        if not tile.get("filename"):
            tile["filename"] = filename_from_url(tile.get("url", ""))
        year = _coerce_year(tile.get("year"))
        if year is None:
            no_year.append(tile)
            continue
        key = _tile_grouping_key(tile)
        groups[key].append((year, tile))

    result = list(no_year)
    for items in groups.values():
        if mode == "latest":
            chosen = max(items, key=lambda x: x[0])
        else:  # oldest
            chosen = min(items, key=lambda x: x[0])
        result.append(chosen[1])
    return result


def filter_by_year_range(tiles, year_min, year_max):
    """Keep only tiles whose 'year' attribute is within [year_min, year_max].

    Tiles missing a year are KEPT (consistent with the never-silently-drop
    principle).

    :param year_min: minimum year (int), or None for no lower bound.
    :param year_max: maximum year (int), or None for no upper bound.
    :return: list of dicts (subset of input).
    """
    if year_min is None and year_max is None:
        return list(tiles)
    result = []
    for tile in tiles:
        year = _coerce_year(tile.get("year"))
        if year is None:
            result.append(tile)
            continue
        if year_min is not None and year < year_min:
            continue
        if year_max is not None and year > year_max:
            continue
        result.append(tile)
    return result


def filter_by_project_value(tiles, target_value):
    """Keep only tiles whose 'project_name' attribute equals target_value.

    Matching is case-insensitive with whitespace tolerance — leading/trailing
    spaces are stripped, and consecutive internal whitespace is collapsed to
    a single space. Tiles with no project_name are DROPPED (this filter is
    used by the LidarBC Program toggle, where unknown-program tiles should
    not be included).

    :param target_value: e.g. "LidarBC Program".
    :return: list of dicts.
    """
    if not target_value:
        return list(tiles)

    def _norm(s):
        if s is None:
            return None
        if not isinstance(s, str):
            s = str(s)
        return " ".join(s.split()).lower()

    target_norm = _norm(target_value)
    result = []
    for tile in tiles:
        val = tile.get("project_name")
        if _norm(val) == target_norm:
            result.append(tile)
    return result


# Characters not allowed in folder names on Windows. Replaced with underscores.
_BAD_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name, fallback="Unknown_Project"):
    """Make a string safe to use as a folder name on any OS.

    - Replaces characters forbidden on Windows with '_'.
    - Strips leading/trailing whitespace, dots, and underscores.
    - Returns the fallback if the cleaned string is empty.
    """
    if name is None:
        return fallback
    if not isinstance(name, str):
        name = str(name)
    cleaned = _BAD_PATH_CHARS.sub("_", name)
    cleaned = cleaned.strip(" .\t\n\r_")
    if not cleaned:
        return fallback
    return cleaned
