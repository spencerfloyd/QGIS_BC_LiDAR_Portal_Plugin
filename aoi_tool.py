# -*- coding: utf-8 -*-
"""
Map tool for drawing straight-edged AOI polygons on the QGIS canvas.

Behavior:
  - Left-click to add a vertex.
  - Right-click (or Enter) to finish the current polygon.
  - Escape to cancel the in-progress polygon.
  - Completed polygons are added as features to an in-memory "LidarBC AOI"
    layer, which the user can also edit/delete via QGIS's normal layer tools.
  - Multiple polygons are supported — each finished polygon becomes its own
    feature in the AOI layer.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand


AOI_LAYER_NAME = "LidarBC AOI"


def get_or_create_aoi_layer():
    """Return the in-memory AOI layer, creating it if needed.

    The layer uses the project CRS so the user can draw in whatever
    coordinate system they're viewing in.
    """
    project = QgsProject.instance()
    for lyr in project.mapLayers().values():
        if lyr.name() == AOI_LAYER_NAME and isinstance(lyr, QgsVectorLayer):
            return lyr

    crs = project.crs()
    uri = f"Polygon?crs={crs.authid()}&field=id:integer"
    layer = QgsVectorLayer(uri, AOI_LAYER_NAME, "memory")
    # Light translucent fill so users can see the basemap underneath
    sym = layer.renderer().symbol()
    sym.setColor(QColor(255, 165, 0, 80))
    sym.symbolLayer(0).setStrokeColor(QColor(255, 100, 0, 255))
    sym.symbolLayer(0).setStrokeWidth(0.6)
    project.addMapLayer(layer)
    return layer


class AOIPolygonTool(QgsMapTool):
    """Click-to-add-vertex polygon tool.

    Emits aoi_finished() when a polygon is completed and committed to the
    AOI layer, so the panel can refresh its tile-count display.
    """

    aoi_finished = pyqtSignal()
    aoi_canceled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.rubber_band = None
        self.temp_band = None  # shows the segment from last vertex to cursor
        self._reset_bands()

    def _reset_bands(self):
        """Discard any in-progress rubber bands and start fresh."""
        if self.rubber_band is not None:
            self.canvas.scene().removeItem(self.rubber_band)
        if self.temp_band is not None:
            self.canvas.scene().removeItem(self.temp_band)

        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 100, 0, 100))
        self.rubber_band.setStrokeColor(QColor(255, 100, 0, 255))
        self.rubber_band.setWidth(2)

        self.temp_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.temp_band.setColor(QColor(255, 100, 0, 180))
        self.temp_band.setWidth(1)

        self.points = []

    def canvasPressEvent(self, event):
        # Convert screen coordinate to map coordinate in the project CRS
        point = self.toMapCoordinates(event.pos())
        if event.button() == Qt.LeftButton:
            self.points.append(QgsPointXY(point))
            # Refresh the rubber band showing the polygon so far
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            for p in self.points:
                self.rubber_band.addPoint(p, False)
            self.rubber_band.show()
        elif event.button() == Qt.RightButton:
            self._finish_polygon()

    def canvasMoveEvent(self, event):
        # Show a "rubber line" from the last clicked vertex to the cursor
        if not self.points:
            return
        cursor_point = QgsPointXY(self.toMapCoordinates(event.pos()))
        self.temp_band.reset(QgsWkbTypes.LineGeometry)
        self.temp_band.addPoint(self.points[-1], False)
        self.temp_band.addPoint(cursor_point, True)
        self.temp_band.show()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish_polygon()
        elif event.key() == Qt.Key_Escape:
            self._cancel_polygon()

    def _finish_polygon(self):
        if len(self.points) < 3:
            # Need at least 3 vertices to form a polygon
            self._cancel_polygon()
            return
        layer = get_or_create_aoi_layer()
        feat = QgsFeature()
        # Build closed polygon ring
        ring = list(self.points) + [self.points[0]]
        feat.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        # Assign a simple incrementing ID
        existing_ids = [f["id"] for f in layer.getFeatures() if f["id"] is not None]
        next_id = (max(existing_ids) + 1) if existing_ids else 1
        feat.setAttributes([next_id])

        layer.dataProvider().addFeatures([feat])
        layer.triggerRepaint()
        self._reset_bands()
        self.aoi_finished.emit()

    def _cancel_polygon(self):
        self._reset_bands()
        self.aoi_canceled.emit()

    def deactivate(self):
        self._reset_bands()
        super().deactivate()
