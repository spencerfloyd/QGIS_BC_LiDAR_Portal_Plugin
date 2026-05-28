# LidarBC Batch Downloader (QGIS Plugin)

Batch-download LiDAR point clouds, DEMs, and DSMs from the LidarBC Open Data
Portal by drawing an area of interest in QGIS.

## What it does

- Adds an **OpenStreetMap basemap** and **all six LidarBC index layers**
  (LiDAR point cloud, DEMs and DSMs at every available scale) to your
  project. The index layers start toggled off — tick them in the layer
  panel when you want to view tile boundaries or open attribute tables.
- Lets you draw one or more **straight-edged polygons** as your area of
  interest.
- Queries the LidarBC ArcGIS REST FeatureServer for tiles that **intersect
  your AOI polygons** (not just their bounding boxes).
- Downloads matching tiles **sequentially** (one file at a time) and
  organizes them by **product** → **project** → **scale**:
  ```
  <your output folder>/
  ├── LiDAR/
  │   ├── LidarBC Program/                ← from "Project Name" attribute
  │   │   └── 1to2500/
  │   │       ├── bc_082e083_..._20250705.laz
  │   │       └── Metadata/
  │   │           └── program_summary.pdf
  │   └── Skeena Forest Inventory/
  │       └── 1to2500/
  │           ├── ...
  │           └── Metadata/
  │               └── metadata.txt        ← placeholder if no PDF available
  ├── DEM/
  │   ├── LidarBC Program/                ← from "oper_name" attribute
  │   │   ├── 1to2500/
  │   │   └── 1to20000/
  │   └── BC Hydro Coverage/
  │       └── 1to2500/
  └── DSM/
      ├── LidarBC Program/
      │   ├── 1to2500/
      │   └── 1to20000/
      └── ...
  ```
- For each LiDAR project, downloads **one metadata PDF** into the
  `Metadata/` subfolder next to its tiles. If no metadata is available
  for a project, writes a `metadata.txt` placeholder instead.
- Lets you filter by **Version** (Latest / Oldest / All) — based on the
  `year` attribute in the layer's table.
- Lets you filter by **Year range** (optional) — pick a single year or
  a span to restrict downloads.
- Lets you filter to **LidarBC Program** files only (optional) — restricts
  to features whose project-name attribute is "LidarBC Program".
- Skips files already on disk from a previous run.
- Deduplicates files that are listed more than once on the server.
- Writes a `failed_downloads.txt` listing any tiles that couldn't be
  fetched, with the reason for each.

## Requirements

- QGIS **3.16 or newer**.
- An internet connection.

No other dependencies — everything uses libraries that ship with QGIS.

## Installation

### Option A — from the official QGIS plugin repository
Once published, search for "LidarBC Batch Downloader" in
*Plugins → Manage and Install Plugins…*

### Option B — from a zip file
1. Download `lidarbc_downloader.zip`.
2. In QGIS, open *Plugins → Manage and Install Plugins… → Install from ZIP*.
3. Browse to the zip and click **Install plugin**.

## Using it

1. Click the **LidarBC** icon in the toolbar (or *Plugins → LidarBC Downloader*).
   The panel docks on the right. The basemap and all six LidarBC index
   layers load automatically the first time, at 50% opacity.
2. Click **Draw AOI polygon**, then left-click on the map to add vertices.
   Right-click (or press Enter) to finish a polygon. Draw additional
   polygons as needed.
3. Tick the products you want under **Products to download**.
4. (Optional) Pick a **Version filter** to handle duplicate tile versions.
5. (Optional) Tick **Only include files within a year range** and set
   the years.
6. Pick (or paste) an **Output folder**. The choice is remembered for
   future sessions.
7. Click **Find tiles in AOI** to see how many tiles match.
8. Click **Download**. Progress is shown in real time. You can click
   **Cancel** at any point — the current file finishes, then the batch
   stops.

## Re-running

If you re-run the same AOI, the plugin notices files already present in
the target folder and skips them. Those are counted as "skipped (already
on disk from a previous run)" in the final summary.

## Failed downloads

Any tile that couldn't be downloaded is recorded in
`failed_downloads.txt` in your output folder, with the filename, source
layer, OBJECTID, URL, and the reason. Re-running the AOI will retry just
those (everything else will be skipped as already-present).

## License

GPL-2.0-or-later.

## Author

Spencer Floyd · Lidar@gov.bc.ca

## Changelog

### 1.7.3
- **Fixed: BCGS Tile Name lookup was failing on the live server.** The
  field discovery only checked internal field names, but on LidarBC the
  visible attribute "BCGS Tile Name" is actually an alias for a
  differently-named internal field. The plugin now matches candidate
  names against BOTH the internal name and the alias, so version-filter
  grouping by BCGS Tile Name works as intended. (Same fix benefits every
  other attribute lookup — year, Project Name, oper_name, etc.)

### 1.7.2
- **Version filter now uses the `BCGS Tile Name` attribute** to identify
  which features represent the same physical tile. Fixes a case where two
  versions of the same tile (e.g. 2019 and 2023) weren't being grouped
  because their filename patterns differed (`xli1m` vs `xl1m`), so both
  were downloaded even with "Latest" selected. With BCGS Tile Name as the
  key, "Latest" / "Oldest" reliably pick a single winner per tile.
- Falls back to the previous filename-based grouping if a row's BCGS Tile
  Name is missing, so the change can never cause data loss.

### 1.7.1
- Index-layer fill opacity reduced from 20% to **10%** for a lighter,
  less obscuring overlay. Outlines remain sharp.

### 1.7.0
- Index layers now have a **translucent fill (20% opacity)** so coverage
  is easier to see at a glance, while the basemap remains visible through
  every tile.
- **Updated layer colors**: LiDAR is red, DEM is green, DSM is blue
  (was green/blue/red).
- Tile outlines are now slightly thicker (0.6 px, up from 0.4 px) so the
  boundary stays prominent against the new fill.

### 1.6.0
- Replaced the welcome popup logo with the clean, transparent original
  (no more pixelation from the upload pipeline's JPEG re-encoding).
- Updated welcome popup text: revised wording, "Lidar" instead of "LiDAR"
  in body copy, restored the LiDAR BC website link.
- Added an official **disclaimer**, copyright notice, and GeoBC
  attribution at the bottom of the welcome popup as small gray fine print,
  with clickable links to the BC copyright page and GeoBC.
- The same disclaimer + copyright + GeoBC attribution now also appears in
  the plugin's "about" text in the QGIS Plugin Manager.
- Plugin manager short description rewritten to match official wording.

### 1.5.0
- Welcome popup now includes the **GeoBC / BRITISH COLUMBIA logo** at the
  top and a link to the LiDAR BC website (https://lidar.gov.bc.ca/).
- Toolbar icon updated to the **BC crest** (sun + mountains).
- Slight wording updates to the welcome popup text.

### 1.4.0
- Added a **welcome popup** that appears the first time the plugin is
  opened, briefly explaining what it does and how to use it.
- The popup has a *"Don't show this again"* checkbox; the choice is
  remembered between sessions.

### 1.3.0
- Year now comes from the **`year` attribute** in each layer's table (no
  more parsing dates from filenames). Both the Version filter and Year
  filter use this.
- Project name now comes from the attribute table:
  - LiDAR layers: **`Project Name`** field
  - DEM/DSM layers: **`oper_name`** field
- **DEM and DSM** are now also organized into project subfolders:
  `DEM/<oper_name>/<scale>/...` and `DSM/<oper_name>/<scale>/...`
- New **LidarBC Program** filter section: tick it to restrict downloads
  to features whose project-name attribute is exactly "LidarBC Program".
  The year filter is greyed out while this is on (Program tiles can span
  multiple years).
- Reference layers no longer load at 50% opacity — they load at 100% but
  with no fill, so the basemap still shows through.
- LiDAR product UI now formatted like DEM/DSM (header label + indented
  scale checkbox).

### 1.2.0
- Index layers now load **toggled off** by default so the panel opens
  quickly. Tick them in the layer panel when you want to see/use them.
- **AOI matching is now polygon-accurate**, not bounding-box: tiles that
  fall outside the drawn polygon (but inside its bounding box) are no
  longer included. For very complex AOIs that exceed the server's URL
  length limit, the plugin falls back to bounding-box matching for the
  affected layers and tells you in the preview.

### 1.1.0
- Auto-load all six LidarBC index layers at 50% opacity (was: only the
  data-extent layer).
- New optional **Year filter** for downloading files within a year span
  or a single year.
- LiDAR `.laz` files are now organized into **project subfolders** (project
  name parsed from the metadata PDF filename).
- Downloads the **metadata PDF** for each LiDAR project into a `Metadata/`
  subfolder; writes a `metadata.txt` placeholder when no PDF is available.
- **Deduplicates** features that point to the same file before download,
  so duplicate server listings aren't reported as "skipped".

### 1.0.0
- Initial release.
