# -*- coding: utf-8 -*-
"""
ArcGIS REST FeatureServer client.

Handles:
 - Discovering the S3 URL field name and tile ID field name at runtime
   (defensive against schema changes / minor field-name variations).
 - Querying layers with a spatial filter (envelope of the AOI polygons).
 - Pagination via resultOffset / resultRecordCount.
 - Returning a list of {'object_id', 'url', 'filename', 'layer_name', ...}
   dicts ready to be filtered for versions.
"""

import json
from urllib import request, parse
from urllib.error import URLError, HTTPError


SERVER_URL = (
    "https://services1.arcgis.com/xeMpV7tU1t4KD3Ei/arcgis/rest/services/"
    "LidarBC_Open_LIDAR/FeatureServer"
)

# Layer IDs are discovered dynamically (see fetch_layers). These are the
# layer NAMES we know about, mapped to a logical product category and scale.
# The mapping is matched case-insensitively against the server's layer names.
KNOWN_LAYERS = {
    # name (lower)                         : (product, scale_label)
    "lidar_point_cloud_index":              ("LiDAR", "1to2500"),
    "lidar_dem_index_1_2_500":              ("DEM",   "1to2500"),
    "lidar_dem_index_1_20_000":             ("DEM",   "1to20000"),
    "lidar_dsm_index_1_2_500":              ("DSM",   "1to2500"),
    "lidar_dsm_index_1_10_000":             ("DSM",   "1to10000"),
    "lidar_dsm_index_1_20_000":             ("DSM",   "1to20000"),
}

# This layer is the reference extent — auto-loaded onto the canvas, never downloaded from
REFERENCE_LAYER_NAME = "lidarproject_data_extent"

# Fields we look for to find the S3 download URL. Order = priority.
URL_FIELD_CANDIDATES = ["s3_url", "s3url", "url", "download_url", "downloadurl"]

# Fields we look for to find the report/metadata PDF URL. Order = priority.
# Only used for LiDAR (point cloud) layers — DEM/DSM don't have metadata PDFs.
RPT_URL_FIELD_CANDIDATES = ["s3rpturl", "s3_rpt_url", "rpt_url", "rpturl",
                            "report_url", "reporturl", "metadata_url", "metadataurl"]

# Fields we look for as a stable tile identifier (used to dedupe across layers
# if needed). Falls back to OBJECTID if none match.
TILE_ID_FIELD_CANDIDATES = ["tile_name", "tilename", "map_tile", "tile_id", "name"]

# BCGS Tile Name — the authoritative tile identifier on every LidarBC layer.
# Used by the version filter to group multiple rows that represent the same
# physical tile (e.g., a 2019 file and a 2023 file of the same map sheet),
# so "Latest" / "Oldest" correctly pick one winner per tile.
BCGS_TILE_NAME_FIELD_CANDIDATES = ["bcgs tile name", "bcgs_tile_name", "bcgstilename"]

# Year attribute — same on all layers per spec.
YEAR_FIELD_CANDIDATES = ["year"]

# Project-name attribute — differs by layer family:
#   LiDAR layers use "Project Name"
#   DEM/DSM layers use "oper_name"
# We try both on every layer and use whichever is present, so the code is
# resilient if the server schema converges in the future. The normalized
# lookup (lowercase, spaces=underscores) makes the match tolerant to minor
# variations like "PROJECT NAME" or "project_name".
PROJECT_NAME_FIELD_CANDIDATES = ["project name", "project_name", "oper_name", "opername"]

# String we match in the project-name attribute when the user enables the
# "Only files from the LidarBC Program" filter. Matched case-insensitively
# with whitespace tolerance.
LIDARBC_PROGRAM_VALUE = "LidarBC Program"

# Reasonable default page size; server caps it at maxRecordCount (often 1000-2000)
DEFAULT_PAGE_SIZE = 1000

# Server CRS used for the spatial query envelope (Web Mercator)
SERVER_WKID = 3857


class ArcGISClientError(Exception):
    """Raised for any error talking to the ArcGIS REST server."""


def _http_get_json(url, params, timeout=60):
    """GET a URL with query params and parse JSON. Raises ArcGISClientError on failure."""
    query = parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = request.Request(full_url, headers={"User-Agent": "LidarBC-QGIS-Plugin/1.0"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as e:
        raise ArcGISClientError(f"HTTP {e.code} from {full_url}") from e
    except URLError as e:
        raise ArcGISClientError(f"Network error contacting {full_url}: {e.reason}") from e
    except Exception as e:
        raise ArcGISClientError(f"Unexpected error contacting {full_url}: {e}") from e

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ArcGISClientError(f"Invalid JSON from {full_url}: {e}") from e

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise ArcGISClientError(f"Server returned error: {msg}")

    return data


def fetch_layers(server_url=SERVER_URL):
    """Get the list of layers on the FeatureServer.

    Returns list of {'id': int, 'name': str} dicts.
    """
    data = _http_get_json(server_url, {"f": "json"})
    layers = data.get("layers", []) or []
    return [{"id": int(L["id"]), "name": L["name"]} for L in layers if "id" in L and "name" in L]


def categorize_layers(layers):
    """Match server layer names against KNOWN_LAYERS.

    Returns:
      categorized: dict mapping (product, scale) -> {'id': int, 'name': str}
      reference: {'id': int, 'name': str} or None for the data-extent layer
      unknown: list of layers we didn't recognize (for diagnostics)
    """
    categorized = {}
    reference = None
    unknown = []
    for L in layers:
        key = L["name"].lower()
        if key in KNOWN_LAYERS:
            categorized[KNOWN_LAYERS[key]] = L
        elif key == REFERENCE_LAYER_NAME:
            reference = L
        else:
            unknown.append(L)
    return categorized, reference, unknown


def _normalize_field_key(s):
    """Normalize a field name for tolerant matching.

    Lowercases, replaces spaces with underscores, and collapses repeated
    underscores. "Project Name" -> "project_name", "PROJECT NAME" -> "project_name".
    """
    if s is None:
        return ""
    s = s.lower().strip().replace(" ", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s


def discover_fields(server_url, layer_id):
    """Get field list for a layer and identify the relevant fields.

    Returns a dict with keys:
      'url'          - download URL field name (required; raises if missing)
      'rpt_url'      - metadata-PDF URL field name, or None
      'tile_id'      - tile identifier field name, or None
      'oid'          - OBJECTID field name
      'year'         - year field name, or None
      'project_name' - project-name field name (Project Name / oper_name), or None
      'all_fields'   - list of all field names on the layer
    """
    data = _http_get_json(f"{server_url}/{layer_id}", {"f": "json"})
    fields = data.get("fields", []) or []
    field_names = [f["name"] for f in fields]
    # ArcGIS fields have two names:
    #   'name'  — the internal field name (no spaces, e.g. "BCGSTileName")
    #   'alias' — the human-readable label (with spaces, e.g. "BCGS Tile Name")
    # The attribute table in QGIS shows the alias. Our candidate field-name
    # lists may come from either source, so we build the lookup from BOTH and
    # always return the internal 'name' (which is what query requests need).
    field_names_lower = {}
    field_names_norm = {}
    for f in fields:
        real_name = f["name"]
        for label in (f.get("name"), f.get("alias")):
            if not label:
                continue
            field_names_lower.setdefault(label.lower(), real_name)
            field_names_norm.setdefault(_normalize_field_key(label), real_name)

    # --- URL field ---
    url_field = None
    for candidate in URL_FIELD_CANDIDATES:
        if candidate in field_names_lower:
            url_field = field_names_lower[candidate]
            break
    if url_field is None:
        for f_lower, f_real in field_names_lower.items():
            if "s3" in f_lower and "url" in f_lower and "rpt" not in f_lower:
                url_field = f_real
                break
    if url_field is None:
        for f_lower, f_real in field_names_lower.items():
            if "url" in f_lower and "rpt" not in f_lower and "report" not in f_lower:
                url_field = f_real
                break
    if url_field is None:
        raise ArcGISClientError(
            f"Could not find a URL field on layer {layer_id}. "
            f"Available fields: {field_names}"
        )

    # --- Report URL field (best-effort) ---
    rpt_url_field = None
    for candidate in RPT_URL_FIELD_CANDIDATES:
        if candidate in field_names_lower:
            rpt_url_field = field_names_lower[candidate]
            break
    if rpt_url_field is None:
        for f_lower, f_real in field_names_lower.items():
            if ("rpt" in f_lower or "report" in f_lower) and "url" in f_lower:
                rpt_url_field = f_real
                break

    # --- Tile ID field (best-effort) ---
    tile_id_field = None
    for candidate in TILE_ID_FIELD_CANDIDATES:
        if candidate in field_names_lower:
            tile_id_field = field_names_lower[candidate]
            break

    # --- Year field ---
    year_field = None
    for candidate in YEAR_FIELD_CANDIDATES:
        normalized = _normalize_field_key(candidate)
        if normalized in field_names_norm:
            year_field = field_names_norm[normalized]
            break

    # --- Project name field ---
    # Try each candidate in order. For LiDAR layers this is "Project Name";
    # for DEM/DSM it's "oper_name". We try both on every layer because the
    # candidate list is shared.
    project_name_field = None
    for candidate in PROJECT_NAME_FIELD_CANDIDATES:
        normalized = _normalize_field_key(candidate)
        if normalized in field_names_norm:
            project_name_field = field_names_norm[normalized]
            break

    # --- BCGS Tile Name field ---
    # Used by the version filter to group versions of the same tile.
    bcgs_tile_name_field = None
    for candidate in BCGS_TILE_NAME_FIELD_CANDIDATES:
        normalized = _normalize_field_key(candidate)
        if normalized in field_names_norm:
            bcgs_tile_name_field = field_names_norm[normalized]
            break

    # --- ObjectID field ---
    oid_field = "OBJECTID"
    for f in fields:
        if f.get("type") == "esriFieldTypeOID":
            oid_field = f["name"]
            break

    return {
        "url": url_field,
        "rpt_url": rpt_url_field,
        "tile_id": tile_id_field,
        "oid": oid_field,
        "year": year_field,
        "project_name": project_name_field,
        "bcgs_tile_name": bcgs_tile_name_field,
        "all_fields": field_names,
    }


def query_layer_by_envelope(
    server_url,
    layer_id,
    envelope,
    fields,
    page_size=DEFAULT_PAGE_SIZE,
    cancel_check=None,
):
    """Query a layer's features whose geometry intersects the given envelope.

    Kept as a fallback for cases where polygon-based queries would exceed
    the server's URL length limit (very complex AOIs with thousands of
    vertices). For straight-edged hand-drawn AOIs, prefer
    query_layer_by_polygon for accurate matching.

    :param envelope: dict with keys xmin, ymin, xmax, ymax (in EPSG:3857).
    :param fields: dict from discover_fields().
    :param cancel_check: optional callable returning True if work should stop.
    :return: list of dicts with 'url', 'rpt_url', 'object_id', 'tile_id',
             'year', 'project_name'. Some values may be None depending on
             which fields the layer has.
    """
    geometry = {
        "xmin": envelope["xmin"],
        "ymin": envelope["ymin"],
        "xmax": envelope["xmax"],
        "ymax": envelope["ymax"],
        "spatialReference": {"wkid": SERVER_WKID},
    }
    return _execute_query(
        server_url, layer_id, geometry, "esriGeometryEnvelope",
        fields, page_size, cancel_check,
    )


def query_layer_by_polygon(
    server_url,
    layer_id,
    rings,
    fields,
    page_size=DEFAULT_PAGE_SIZE,
    cancel_check=None,
):
    """Query a layer's features whose geometry intersects the given polygon.

    Uses ArcGIS REST's polygon spatial filter so tiles outside the polygon
    but inside its bounding box are NOT returned.

    :param rings: list of rings, each a list of [x, y] coordinate pairs in
        EPSG:3857. ArcGIS polygon convention: outer rings clockwise, inner
        rings counter-clockwise. For a simple polygon, a single ring works
        and the server is forgiving about winding order in practice.
    :param fields: dict from discover_fields().
    :param cancel_check: optional callable returning True if work should stop.
    :return: list of dicts (same structure as query_layer_by_envelope).
    """
    geometry = {
        "rings": rings,
        "spatialReference": {"wkid": SERVER_WKID},
    }
    return _execute_query(
        server_url, layer_id, geometry, "esriGeometryPolygon",
        fields, page_size, cancel_check,
    )


def _execute_query(
    server_url, layer_id, geometry, geometry_type, fields,
    page_size, cancel_check,
):
    """Run a paginated spatial query and return assembled feature dicts.

    Shared implementation for both envelope- and polygon-based queries.
    """
    # Build the list of fields to request. Always include OID and URL;
    # include the optional ones only if they're present on this layer.
    out_fields = [fields["oid"], fields["url"]]
    for opt_key in ("rpt_url", "tile_id", "year", "project_name", "bcgs_tile_name"):
        if fields.get(opt_key):
            out_fields.append(fields[opt_key])
    # Dedupe while preserving order (some layers might have the same field
    # serving two roles, though unlikely):
    seen = set()
    out_fields_uniq = []
    for f in out_fields:
        if f and f not in seen:
            seen.add(f)
            out_fields_uniq.append(f)

    base_params = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps(geometry),
        "geometryType": geometry_type,
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": str(SERVER_WKID),
        "outFields": ",".join(out_fields_uniq),
        "returnGeometry": "false",
        "resultRecordCount": str(page_size),
    }

    results = []
    offset = 0
    while True:
        if cancel_check and cancel_check():
            break
        params = dict(base_params)
        params["resultOffset"] = str(offset)
        data = _http_get_json(f"{server_url}/{layer_id}/query", params)
        features = data.get("features", []) or []
        for feat in features:
            attrs = feat.get("attributes", {}) or {}
            url_val = attrs.get(fields["url"])
            rpt_val = attrs.get(fields["rpt_url"]) if fields.get("rpt_url") else None
            tile_id_val = attrs.get(fields["tile_id"]) if fields.get("tile_id") else None
            year_val = attrs.get(fields["year"]) if fields.get("year") else None
            project_name_val = (
                attrs.get(fields["project_name"]) if fields.get("project_name") else None
            )
            bcgs_tile_name_val = (
                attrs.get(fields["bcgs_tile_name"]) if fields.get("bcgs_tile_name") else None
            )
            record = {
                "object_id": attrs.get(fields["oid"]),
                "url": url_val,
                "rpt_url": rpt_val,
                "tile_id": tile_id_val,
                "year": year_val,
                "project_name": project_name_val,
                "bcgs_tile_name": bcgs_tile_name_val,
                "missing_url": not bool(url_val),
            }
            results.append(record)
        # Pagination: ArcGIS sets exceededTransferLimit when there's more
        if data.get("exceededTransferLimit") and features:
            offset += len(features)
            continue
        # Some servers don't set the flag; fall back to "page filled exactly"
        if len(features) == page_size:
            offset += len(features)
            continue
        break

    return results


def aoi_envelope_3857(aoi_geometries_3857):
    """Compute the combined envelope of a list of QgsGeometry already in EPSG:3857.

    Pure-data helper for testability. Accepts an iterable of
    {'xmin','ymin','xmax','ymax'} envelope dicts.
    """
    if not aoi_geometries_3857:
        raise ValueError("No AOI geometries provided")
    xmin = min(e["xmin"] for e in aoi_geometries_3857)
    ymin = min(e["ymin"] for e in aoi_geometries_3857)
    xmax = max(e["xmax"] for e in aoi_geometries_3857)
    ymax = max(e["ymax"] for e in aoi_geometries_3857)
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
