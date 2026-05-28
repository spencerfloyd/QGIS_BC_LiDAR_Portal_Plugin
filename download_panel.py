# -*- coding: utf-8 -*-
"""
Dockable panel for the LidarBC Batch Downloader.

UI sections:
  1. Reference / basemap controls (auto-loads OSM + all 6 LidarBC index layers
     at 50% opacity so users can browse the layers and their attribute tables)
  2. Draw AOI button + AOI count
  3. Product selection (LiDAR / DEM / DSM with scale sub-options)
  4. Version filter (Latest / Oldest / All)
  5. Year filter (optional; "all years" by default, or specify min/max year)
  6. Output folder picker (remembered via QSettings)
  7. Find Tiles button + preview count
  8. Download button + progress bar + status label + Cancel button
"""

import os

from qgis.PyQt.QtCore import Qt, QSettings, QThread, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from .aoi_tool import AOIPolygonTool, get_or_create_aoi_layer, AOI_LAYER_NAME
from .arcgis_client import (
    KNOWN_LAYERS,
    SERVER_URL,
    SERVER_WKID,
    ArcGISClientError,
    LIDARBC_PROGRAM_VALUE,
    categorize_layers,
    discover_fields,
    fetch_layers,
    query_layer_by_envelope,
    query_layer_by_polygon,
)
from .download_worker import DownloadWorker
from .version_filter import (
    filename_from_url,
    filter_by_project_value,
    filter_by_year_range,
    filter_versions,
    sanitize_folder_name,
)


SETTINGS_GROUP = "LidarBCDownloader"
SETTING_OUTPUT_DIR = f"{SETTINGS_GROUP}/output_dir"


BASEMAP_LAYER_NAME = "OpenStreetMap (basemap)"

# Friendly display names for the index layers in the QGIS layer tree
LAYER_DISPLAY_NAMES = {
    ("LiDAR", "1to2500"):  "LidarBC — LiDAR Point Cloud Index (1:2,500)",
    ("DEM",   "1to2500"):  "LidarBC — DEM Index (1:2,500)",
    ("DEM",   "1to20000"): "LidarBC — DEM Index (1:20,000)",
    ("DSM",   "1to2500"):  "LidarBC — DSM Index (1:2,500)",
    ("DSM",   "1to10000"): "LidarBC — DSM Index (1:10,000)",
    ("DSM",   "1to20000"): "LidarBC — DSM Index (1:20,000)",
}

# Distinct stroke colors per product family so layers are visually
# distinguishable when stacked. Fills use the same color at 20% opacity
# (alpha=51 of 255) to make coverage easier to see at a glance.
LAYER_STROKE_COLORS = {
    "LiDAR": QColor(180, 50, 50, 255),   # red
    "DEM":   QColor(0, 120, 0, 255),     # green
    "DSM":   QColor(30, 90, 200, 255),   # blue
}


class DownloadPanel(QDockWidget):
    """Right-docked panel containing the plugin UI."""

    def __init__(self, iface, plugin):
        super().__init__("LidarBC Batch Downloader")
        self.iface = iface
        self.plugin = plugin
        self.setObjectName("LidarBCDownloadPanel")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # State
        self._aoi_tool = None
        self._previous_tool = None
        self._matched_jobs = []
        self._server_layers_cache = None
        self._worker = None
        self._worker_thread = None

        self._build_ui()

    # ---------- UI construction ----------

    def _build_ui(self):
        outer = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(8)

        # --- Reference & basemap ---
        ref_group = QGroupBox("Reference layers")
        ref_layout = QVBoxLayout()
        self.btn_load_ref = QPushButton("Load basemap + LidarBC index layers")
        self.btn_load_ref.clicked.connect(self.load_reference_layer)
        ref_layout.addWidget(self.btn_load_ref)
        ref_help = QLabel(
            "Loads an OpenStreetMap basemap and all six LidarBC index layers\n"
            "so you can browse coverage and attribute tables. Layers start\n"
            "toggled OFF for performance — tick them in the layer panel when\n"
            "you want to view tile boundaries or attributes."
        )
        ref_help.setWordWrap(True)
        ref_help.setStyleSheet("color: gray;")
        ref_layout.addWidget(ref_help)
        ref_group.setLayout(ref_layout)
        layout.addWidget(ref_group)

        # --- AOI ---
        aoi_group = QGroupBox("Area of interest")
        aoi_layout = QVBoxLayout()
        self.btn_draw = QPushButton("Draw AOI polygon")
        self.btn_draw.setCheckable(True)
        self.btn_draw.clicked.connect(self._toggle_draw_tool)
        aoi_layout.addWidget(self.btn_draw)
        self.lbl_aoi_count = QLabel("AOI polygons: 0")
        aoi_layout.addWidget(self.lbl_aoi_count)
        self.btn_clear_aoi = QPushButton("Clear all AOIs")
        self.btn_clear_aoi.clicked.connect(self._clear_aois)
        aoi_layout.addWidget(self.btn_clear_aoi)
        aoi_help = QLabel(
            "Left-click adds a vertex. Right-click (or Enter) finishes the\n"
            "polygon. Escape cancels. Draw as many polygons as you need."
        )
        aoi_help.setWordWrap(True)
        aoi_help.setStyleSheet("color: gray;")
        aoi_layout.addWidget(aoi_help)
        aoi_group.setLayout(aoi_layout)
        layout.addWidget(aoi_group)

        # --- Products ---
        prod_group = QGroupBox("Products to download")
        prod_layout = QVBoxLayout()
        prod_layout.addWidget(QLabel("LiDAR:"))
        self.cb_lidar_2500 = QCheckBox("    1:2,500")
        prod_layout.addWidget(self.cb_lidar_2500)
        prod_layout.addWidget(QLabel("DEM:"))
        self.cb_dem_2500 = QCheckBox("    1:2,500")
        self.cb_dem_20000 = QCheckBox("    1:20,000")
        prod_layout.addWidget(self.cb_dem_2500)
        prod_layout.addWidget(self.cb_dem_20000)
        prod_layout.addWidget(QLabel("DSM:"))
        self.cb_dsm_2500 = QCheckBox("    1:2,500")
        self.cb_dsm_10000 = QCheckBox("    1:10,000")
        self.cb_dsm_20000 = QCheckBox("    1:20,000")
        prod_layout.addWidget(self.cb_dsm_2500)
        prod_layout.addWidget(self.cb_dsm_10000)
        prod_layout.addWidget(self.cb_dsm_20000)
        prod_group.setLayout(prod_layout)
        layout.addWidget(prod_group)

        # --- Version filter ---
        ver_group = QGroupBox("Version filter")
        ver_layout = QVBoxLayout()
        ver_layout.addWidget(QLabel("If a tile has multiple versions, keep:"))
        self.combo_version = QComboBox()
        self.combo_version.addItems(["Latest", "Oldest", "All versions"])
        ver_layout.addWidget(self.combo_version)
        ver_help = QLabel(
            "Based on the 'year' attribute in the layer's table."
        )
        ver_help.setWordWrap(True)
        ver_help.setStyleSheet("color: gray;")
        ver_layout.addWidget(ver_help)
        ver_group.setLayout(ver_layout)
        layout.addWidget(ver_group)

        # --- Year filter ---
        self.year_group = QGroupBox("Year filter (optional)")
        year_layout = QVBoxLayout()
        self.cb_year_filter = QCheckBox("Only include files within a year range")
        self.cb_year_filter.toggled.connect(self._on_year_filter_toggled)
        year_layout.addWidget(self.cb_year_filter)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("From year:"))
        self.spin_year_min = QSpinBox()
        self.spin_year_min.setRange(1990, 2100)
        self.spin_year_min.setValue(2015)
        self.spin_year_min.setEnabled(False)
        spin_row.addWidget(self.spin_year_min)
        spin_row.addSpacing(8)
        spin_row.addWidget(QLabel("to year:"))
        self.spin_year_max = QSpinBox()
        self.spin_year_max.setRange(1990, 2100)
        self.spin_year_max.setValue(2026)
        self.spin_year_max.setEnabled(False)
        spin_row.addWidget(self.spin_year_max)
        year_layout.addLayout(spin_row)

        year_help = QLabel(
            "Set both years to the same value to filter to a single year.\n"
            "Year is taken from the 'year' attribute in the layer's table."
        )
        year_help.setWordWrap(True)
        year_help.setStyleSheet("color: gray;")
        year_layout.addWidget(year_help)
        self.year_group.setLayout(year_layout)
        layout.addWidget(self.year_group)

        # --- LidarBC Program filter ---
        program_group = QGroupBox("LidarBC Program filter (optional)")
        program_layout = QVBoxLayout()
        self.cb_program_filter = QCheckBox("Only include files from the LidarBC Program")
        self.cb_program_filter.toggled.connect(self._on_program_filter_toggled)
        program_layout.addWidget(self.cb_program_filter)
        program_help = QLabel(
            "This only downloads files that were collected in the current\n"
            "BC LiDAR Program that began in 2023.\n"
            "(Year filter is disabled while this is on, because Program tiles\n"
            "may have multi-year coverage.)"
        )
        program_help.setWordWrap(True)
        program_help.setStyleSheet("color: gray;")
        program_layout.addWidget(program_help)
        program_group.setLayout(program_layout)
        layout.addWidget(program_group)

        # --- Output folder ---
        out_group = QGroupBox("Output folder")
        out_layout = QHBoxLayout()
        self.edit_output = QLineEdit()
        last_dir = QSettings().value(SETTING_OUTPUT_DIR, "", type=str)
        if last_dir:
            self.edit_output.setText(last_dir)
        out_layout.addWidget(self.edit_output)
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse_output)
        out_layout.addWidget(self.btn_browse)
        out_group.setLayout(out_layout)
        layout.addWidget(out_group)

        # --- Find tiles ---
        self.btn_find = QPushButton("Find tiles in AOI")
        self.btn_find.clicked.connect(self._find_tiles)
        layout.addWidget(self.btn_find)
        self.lbl_preview = QLabel("")
        self.lbl_preview.setWordWrap(True)
        layout.addWidget(self.lbl_preview)

        # --- Download ---
        self.btn_download = QPushButton("Download")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._start_download)
        layout.addWidget(self.btn_download)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        self.btn_cancel = QPushButton("Cancel (after current file)")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_download)
        layout.addWidget(self.btn_cancel)

        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        scroll.setWidget(container)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll)
        self.setWidget(outer)

        # Keep AOI count in sync
        QgsProject.instance().layersAdded.connect(self._refresh_aoi_count)
        QgsProject.instance().layersRemoved.connect(self._refresh_aoi_count)

    def _on_year_filter_toggled(self, checked):
        self.spin_year_min.setEnabled(checked)
        self.spin_year_max.setEnabled(checked)

    def _on_program_filter_toggled(self, checked):
        # When the Program filter is on, the year filter is meaningless
        # because Program tiles can span multiple years. Disable the whole
        # year-filter group so it's clearly inactive.
        self.year_group.setEnabled(not checked)
        if checked:
            # Also uncheck the year-filter box so the disabled-state visual
            # matches the actual filtering behavior.
            self.cb_year_filter.setChecked(False)

    # ---------- Reference layers ----------

    def load_reference_layer(self):
        """Add the OSM basemap and all six LidarBC index layers."""
        project = QgsProject.instance()

        # OSM XYZ basemap
        if not any(L.name() == BASEMAP_LAYER_NAME for L in project.mapLayers().values()):
            osm_uri = (
                "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
                "&zmin=0&zmax=19"
            )
            osm_layer = QgsRasterLayer(osm_uri, BASEMAP_LAYER_NAME, "wms")
            if osm_layer.isValid():
                project.addMapLayer(osm_layer)
                # Send the basemap to the bottom of the layer tree
                root = project.layerTreeRoot()
                node = root.findLayer(osm_layer.id())
                if node is not None:
                    clone = node.clone()
                    parent = node.parent()
                    parent.insertChildNode(-1, clone)
                    parent.removeChildNode(node)
            else:
                self.iface.messageBar().pushWarning(
                    "LidarBC Downloader",
                    "Could not load OpenStreetMap basemap (no internet?).",
                )

        # Load all six downloadable index layers
        try:
            server_layers = self._cached_server_layers()
        except ArcGISClientError as e:
            self.iface.messageBar().pushWarning(
                "LidarBC Downloader",
                f"Could not query the LidarBC server: {e}",
            )
            return

        categorized, _, _ = categorize_layers(server_layers)

        existing_names = {L.name() for L in project.mapLayers().values()}

        for key, srv_layer in categorized.items():
            display_name = LAYER_DISPLAY_NAMES.get(key, srv_layer["name"])
            if display_name in existing_names:
                continue
            uri = (
                f"crs='EPSG:{SERVER_WKID}' "
                f"url='{SERVER_URL}/{srv_layer['id']}'"
            )
            qgs_layer = QgsVectorLayer(uri, display_name, "arcgisfeatureserver")
            if not qgs_layer.isValid():
                self.iface.messageBar().pushWarning(
                    "LidarBC Downloader",
                    f"Could not load {display_name} — check internet connection.",
                )
                continue
            # Translucent fill (10% opacity) makes coverage easy to see at a
            # glance, while the basemap remains visible through every tile.
            # The outline stays sharp at full opacity so tile boundaries are
            # still clearly defined.
            try:
                sym = qgs_layer.renderer().symbol()
                stroke_color = LAYER_STROKE_COLORS.get(key[0], QColor(60, 60, 60, 255))
                # Fill = same hue as stroke at 10% alpha (26 of 255)
                fill_color = QColor(stroke_color.red(),
                                    stroke_color.green(),
                                    stroke_color.blue(),
                                    26)
                sym.setColor(fill_color)
                sym.symbolLayer(0).setStrokeColor(stroke_color)
                sym.symbolLayer(0).setStrokeWidth(0.6)
                qgs_layer.triggerRepaint()
            except (AttributeError, IndexError):
                # Style customization is best-effort; the layer still works without it
                pass
            # Add to project but turn the tree node off so the layer doesn't
            # render or fetch tiles until the user explicitly enables it.
            # Each AGOL layer can take a few seconds to draw, so this keeps
            # the initial panel-open fast.
            project.addMapLayer(qgs_layer)
            tree_root = project.layerTreeRoot()
            tree_node = tree_root.findLayer(qgs_layer.id())
            if tree_node is not None:
                tree_node.setItemVisibilityChecked(False)

    def _cached_server_layers(self):
        if self._server_layers_cache is None:
            self._server_layers_cache = fetch_layers(SERVER_URL)
        return self._server_layers_cache

    # ---------- AOI drawing ----------

    def _toggle_draw_tool(self, checked):
        canvas = self.iface.mapCanvas()
        if checked:
            get_or_create_aoi_layer()
            self._aoi_tool = AOIPolygonTool(canvas)
            self._aoi_tool.aoi_finished.connect(self._refresh_aoi_count)
            self._previous_tool = canvas.mapTool()
            canvas.setMapTool(self._aoi_tool)
        else:
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)
            self._aoi_tool = None

    def _refresh_aoi_count(self, *args):
        layer = self._aoi_layer()
        n = layer.featureCount() if layer is not None else 0
        self.lbl_aoi_count.setText(f"AOI polygons: {n}")

    def _aoi_layer(self):
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name() == AOI_LAYER_NAME and isinstance(lyr, QgsVectorLayer):
                return lyr
        return None

    def _clear_aois(self):
        layer = self._aoi_layer()
        if layer is None:
            return
        layer.dataProvider().truncate()
        layer.triggerRepaint()
        self._refresh_aoi_count()
        self._matched_jobs = []
        self.btn_download.setEnabled(False)
        self.lbl_preview.setText("")

    # ---------- Find tiles ----------

    def _selected_layer_keys(self):
        keys = []
        if self.cb_lidar_2500.isChecked():
            keys.append(("LiDAR", "1to2500"))
        if self.cb_dem_2500.isChecked():
            keys.append(("DEM", "1to2500"))
        if self.cb_dem_20000.isChecked():
            keys.append(("DEM", "1to20000"))
        if self.cb_dsm_2500.isChecked():
            keys.append(("DSM", "1to2500"))
        if self.cb_dsm_10000.isChecked():
            keys.append(("DSM", "1to10000"))
        if self.cb_dsm_20000.isChecked():
            keys.append(("DSM", "1to20000"))
        return keys

    def _aoi_envelope_in_3857(self):
        layer = self._aoi_layer()
        if layer is None or layer.featureCount() == 0:
            return None
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem(f"EPSG:{SERVER_WKID}")
        transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())

        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            g = QgsGeometry(geom)
            g.transform(transform)
            bbox = g.boundingBox()
            xmin = min(xmin, bbox.xMinimum())
            ymin = min(ymin, bbox.yMinimum())
            xmax = max(xmax, bbox.xMaximum())
            ymax = max(ymax, bbox.yMaximum())
        if xmin == float("inf"):
            return None
        return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}

    def _aoi_polygons_in_3857(self):
        """Return a list of polygon rings (one per AOI feature) in EPSG:3857.

        Each polygon is a list-of-rings as ArcGIS expects: outer ring first,
        then any inner (hole) rings. Hand-drawn AOIs from this plugin won't
        have holes, but the code handles them defensively in case the user
        edits the AOI layer through QGIS's normal tools.

        Returns: list[ list[ list[ [x,y] ] ] ]
                  └ polygons └ rings └ vertices
        """
        layer = self._aoi_layer()
        if layer is None or layer.featureCount() == 0:
            return []
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem(f"EPSG:{SERVER_WKID}")
        transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())

        polygons = []
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            g = QgsGeometry(geom)
            g.transform(transform)
            # asMultiPolygon handles both single- and multi-polygon geometries
            if g.isMultipart():
                parts = g.asMultiPolygon()
            else:
                parts = [g.asPolygon()]
            for poly in parts:
                # poly is a list of rings; each ring is a list of QgsPointXY
                rings = []
                for ring in poly:
                    if len(ring) < 3:
                        continue
                    coords = [[pt.x(), pt.y()] for pt in ring]
                    # Ensure ring is closed (first == last); ArcGIS requires it
                    if coords[0] != coords[-1]:
                        coords.append(list(coords[0]))
                    rings.append(coords)
                if rings:
                    polygons.append(rings)
        return polygons

    def _polygon_bbox(self, rings):
        """Compute the bounding box of a polygon ring list as ArcGIS envelope dict."""
        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")
        for ring in rings:
            for pt in ring:
                x, y = pt[0], pt[1]
                if x < xmin: xmin = x
                if y < ymin: ymin = y
                if x > xmax: xmax = x
                if y > ymax: ymax = y
        return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}

    def _find_tiles(self):
        selected = self._selected_layer_keys()
        if not selected:
            QMessageBox.warning(
                self, "Nothing selected",
                "Please tick at least one product to download.",
            )
            return

        envelope = self._aoi_envelope_in_3857()
        if envelope is None:
            QMessageBox.warning(
                self, "No AOI",
                "Please draw at least one AOI polygon before searching for tiles.",
            )
            return

        aoi_polygons = self._aoi_polygons_in_3857()
        # aoi_polygons should be non-empty whenever envelope is non-None, but
        # guard against any edge case (e.g. invalid geometries):
        if not aoi_polygons:
            QMessageBox.warning(
                self, "No AOI",
                "AOI polygons could not be read. Try redrawing them.",
            )
            return

        out_dir = self.edit_output.text().strip()
        if out_dir:
            QSettings().setValue(SETTING_OUTPUT_DIR, out_dir)

        self.lbl_preview.setText("Querying server… (this may take a moment for large AOIs)")
        QApplication.processEvents()

        try:
            server_layers = self._cached_server_layers()
            categorized, _, _ = categorize_layers(server_layers)
        except ArcGISClientError as e:
            QMessageBox.critical(self, "Server error", f"Could not query the server:\n\n{e}")
            self.lbl_preview.setText("")
            return

        version_mode = self.combo_version.currentText().lower().split()[0]
        year_min = None
        year_max = None
        if self.cb_year_filter.isChecked() and not self.cb_program_filter.isChecked():
            year_min = self.spin_year_min.value()
            year_max = self.spin_year_max.value()
            if year_min > year_max:
                QMessageBox.warning(
                    self, "Year range invalid",
                    f"'From year' ({year_min}) is greater than 'to year' ({year_max}).",
                )
                self.lbl_preview.setText("")
                return

        program_only = self.cb_program_filter.isChecked()

        jobs = []
        per_layer_counts = {}
        missing_url_count = 0
        missing_rpt_url_count = 0
        missing_project_name_count = 0
        layers_missing_project_field = []  # layer names where 'project_name' field wasn't found
        fallback_to_envelope_layers = []  # layers that hit URL-length limits

        for key in selected:
            if key not in categorized:
                self.iface.messageBar().pushWarning(
                    "LidarBC Downloader",
                    f"Layer for {key[0]} {key[1]} not found on server — skipping.",
                )
                continue
            srv_layer = categorized[key]
            try:
                fields = discover_fields(SERVER_URL, srv_layer["id"])
            except ArcGISClientError as e:
                QMessageBox.critical(
                    self, "Server error",
                    f"Error querying layer {srv_layer['name']}:\n\n{e}",
                )
                continue

            # If user requested Program-only but this layer has no project_name
            # field, we can't filter; warn and skip that layer.
            if program_only and not fields.get("project_name"):
                layers_missing_project_field.append(srv_layer["name"])
                continue
            # Track layers where we just can't tell the project — affects folder naming
            if not fields.get("project_name"):
                layers_missing_project_field.append(srv_layer["name"])

            # Query once per AOI polygon (so we get tiles that actually
            # intersect the polygon, not just its bounding box). Dedupe by
            # OBJECTID across the polygon-level results, since AOIs may
            # overlap.
            features_by_oid = {}
            used_envelope_fallback = False
            for poly_rings in aoi_polygons:
                try:
                    poly_features = query_layer_by_polygon(
                        SERVER_URL,
                        srv_layer["id"],
                        poly_rings,
                        fields,
                    )
                except ArcGISClientError as e:
                    # If the polygon was too complex for the URL length limit,
                    # fall back to envelope-based query for that polygon. This
                    # over-includes (returns tiles in the bbox even if outside
                    # the polygon) but is better than failing the whole query.
                    msg = str(e).lower()
                    if ("uri" in msg or "url" in msg or "too long" in msg
                            or "414" in msg or "400" in msg):
                        bbox = self._polygon_bbox(poly_rings)
                        try:
                            poly_features = query_layer_by_envelope(
                                SERVER_URL,
                                srv_layer["id"],
                                bbox,
                                fields,
                            )
                            used_envelope_fallback = True
                        except ArcGISClientError as e2:
                            QMessageBox.critical(
                                self, "Server error",
                                f"Error querying layer {srv_layer['name']}:\n\n{e2}",
                            )
                            continue
                    else:
                        QMessageBox.critical(
                            self, "Server error",
                            f"Error querying layer {srv_layer['name']}:\n\n{e}",
                        )
                        continue

                for feat in poly_features:
                    oid = feat.get("object_id")
                    dedup_key = oid if oid is not None else feat.get("url")
                    if dedup_key in features_by_oid:
                        continue
                    features_by_oid[dedup_key] = feat

            if used_envelope_fallback:
                fallback_to_envelope_layers.append(srv_layer["name"])

            features = list(features_by_oid.values())

            # --- Apply Program filter at the feature level ---
            # Doing this BEFORE building job dicts lets us count how many
            # features were dropped because of it (informational).
            if program_only:
                features = filter_by_project_value(features, LIDARBC_PROGRAM_VALUE)

            layer_jobs = []
            is_lidar = (key[0] == "LiDAR")
            for feat in features:
                if feat.get("missing_url"):
                    missing_url_count += 1
                    continue
                fname = filename_from_url(feat["url"])
                rpt_url = feat.get("rpt_url")
                # Project name comes from the attribute now (Project Name for LiDAR,
                # oper_name for DEM/DSM — discover_fields handles both)
                raw_project = feat.get("project_name")
                project_folder = sanitize_folder_name(raw_project)
                if raw_project in (None, ""):
                    missing_project_name_count += 1
                # Track LiDAR features missing their report URL — those projects
                # will get a metadata.txt placeholder instead of a PDF.
                if is_lidar and not rpt_url:
                    missing_rpt_url_count += 1
                layer_jobs.append({
                    "url": feat["url"],
                    "filename": fname,
                    "product": key[0],
                    "scale": key[1],
                    "layer_name": srv_layer["name"],
                    "object_id": feat["object_id"],
                    "year": feat.get("year"),
                    "bcgs_tile_name": feat.get("bcgs_tile_name"),
                    "rpt_url": rpt_url if is_lidar else None,
                    "project": project_folder,
                })

            # Apply version filter per layer (uses 'year' attribute)
            filtered = filter_versions(layer_jobs, version_mode)
            # Apply year filter per layer (skipped if program_only)
            filtered = filter_by_year_range(filtered, year_min, year_max)

            per_layer_counts[srv_layer["name"]] = len(filtered)
            jobs.extend(filtered)

        # --- Deduplicate by target path ---
        # Some features genuinely point to the same file (duplicate rows on the
        # server, or the same tile listed under more than one scale by mistake).
        # We dedupe before download so the user doesn't see misleading
        # "skipped" counts for files we already queued earlier in the SAME run.
        seen_targets = set()
        deduped = []
        duplicate_count = 0
        for job in jobs:
            # Same target-path computation as DownloadWorker._target_path
            proj = job.get("project") or "Unknown_Project"
            target = os.path.join(job["product"], proj, job["scale"], job["filename"])
            if target in seen_targets:
                duplicate_count += 1
                continue
            seen_targets.add(target)
            deduped.append(job)
        jobs = deduped

        self._matched_jobs = jobs

        if not jobs:
            self.lbl_preview.setText(
                "No matching tiles found in the AOI for the selected products."
            )
            self.btn_download.setEnabled(False)
            return

        # Count distinct projects across ALL products (LiDAR, DEM, DSM)
        all_projects = {j.get("project") or "Unknown_Project" for j in jobs}
        lidar_projects = {j.get("project") or "Unknown_Project"
                          for j in jobs if j["product"] == "LiDAR"}

        lines = [f"<b>{len(jobs)}</b> tiles match across {len(per_layer_counts)} layer(s):"]
        for name, count in per_layer_counts.items():
            lines.append(f"&nbsp;&nbsp;• {name}: {count}")
        if program_only:
            lines.append(
                "<br>Filtered to <b>LidarBC Program</b> files only."
            )
        if all_projects:
            lines.append(
                f"<br>Tiles span <b>{len(all_projects)}</b> project(s) total."
            )
        if lidar_projects:
            lines.append(
                f"&nbsp;&nbsp;• LiDAR: {len(lidar_projects)} project(s); "
                f"one metadata PDF will be downloaded per project."
            )
        if duplicate_count:
            lines.append(
                f"<br><i>{duplicate_count}</i> duplicate listing(s) removed "
                f"(same file listed more than once on the server)."
            )
        if missing_url_count:
            lines.append(
                f"<br><i>{missing_url_count}</i> feature(s) had no download URL "
                f"and were skipped."
            )
        if missing_rpt_url_count:
            lines.append(
                f"<br><i>{missing_rpt_url_count}</i> LiDAR feature(s) had no metadata-PDF URL; "
                f"those projects will get a metadata.txt placeholder."
            )
        if missing_project_name_count:
            lines.append(
                f"<br><i>{missing_project_name_count}</i> feature(s) had no project name; "
                f"those will go in an <i>Unknown_Project</i> folder."
            )
        if program_only and layers_missing_project_field:
            lines.append(
                "<br><i>Warning:</i> the following layer(s) had no project-name "
                "field and were skipped for Program filtering: "
                + ", ".join(sorted(set(layers_missing_project_field)))
            )
        if fallback_to_envelope_layers:
            lines.append(
                "<br><i>Note:</i> the AOI polygon was too complex for an exact "
                "match on layer(s): "
                + ", ".join(sorted(set(fallback_to_envelope_layers)))
                + ". Bounding-box matching was used for those, which may "
                "include some tiles outside the polygon."
            )
        self.lbl_preview.setText("<br>".join(lines))
        self.btn_download.setEnabled(True)

    # ---------- Downloading ----------

    def _browse_output(self):
        start = self.edit_output.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if chosen:
            self.edit_output.setText(chosen)
            QSettings().setValue(SETTING_OUTPUT_DIR, chosen)

    def _start_download(self):
        if not self._matched_jobs:
            return
        out_dir = self.edit_output.text().strip()
        if not out_dir:
            QMessageBox.warning(
                self, "No output folder",
                "Please choose an output folder before downloading.",
            )
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Output folder error",
                                 f"Could not create {out_dir}:\n{e}")
            return

        QSettings().setValue(SETTING_OUTPUT_DIR, out_dir)

        self._worker_thread = QThread()
        self._worker = DownloadWorker(self._matched_jobs, out_dir)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.message.connect(self._on_message)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._worker_thread.quit)

        self.btn_download.setEnabled(False)
        self.btn_find.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        # Progress total is set by the first signal; rough estimate here
        self.progress.setRange(0, max(1, len(self._matched_jobs)))
        self.progress.setValue(0)
        self.lbl_status.setText("Starting…")

        self._worker_thread.start()

    def _on_progress(self, current, total, filename):
        if self.progress.maximum() != total:
            self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.lbl_status.setText(f"[{current}/{total}] {filename}")

    def _on_message(self, msg):
        self.lbl_status.setText(msg)

    def _on_finished(self, succeeded, failed, skipped, failed_log_path):
        self.btn_download.setEnabled(True)
        self.btn_find.setEnabled(True)
        self.btn_cancel.setEnabled(False)

        summary = (
            f"Done.\n\n"
            f"  Downloaded: {succeeded}\n"
            f"  Skipped (already on disk from a previous run): {skipped}\n"
            f"  Failed: {failed}\n"
        )
        if failed_log_path:
            summary += f"\nFailures logged to:\n  {failed_log_path}"
        self.lbl_status.setText(summary)

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Download complete")
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setText(summary)
        if failed_log_path:
            open_btn = msg_box.addButton("Open failure log folder", QMessageBox.ActionRole)
        else:
            open_btn = None
        msg_box.addButton(QMessageBox.Ok)
        msg_box.exec_()
        if open_btn is not None and msg_box.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(failed_log_path)))

        if self._worker_thread is not None:
            self._worker_thread.wait(2000)
            self._worker_thread.deleteLater()
            self._worker_thread = None
        self._worker = None

    def _cancel_download(self):
        if self._worker is not None:
            self._worker.request_cancel()
            self.lbl_status.setText("Cancel requested — finishing current file…")
            self.btn_cancel.setEnabled(False)

    # ---------- Cleanup ----------

    def cleanup(self):
        try:
            QgsProject.instance().layersAdded.disconnect(self._refresh_aoi_count)
        except (TypeError, RuntimeError):
            pass
        try:
            QgsProject.instance().layersRemoved.disconnect(self._refresh_aoi_count)
        except (TypeError, RuntimeError):
            pass
        if self._worker is not None:
            self._worker.request_cancel()
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
