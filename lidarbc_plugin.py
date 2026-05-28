# -*- coding: utf-8 -*-
"""
Main plugin class for LidarBC Batch Downloader.
Registers the toolbar icon, menu entry, and dockable panel.
"""

import os.path

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .download_panel import DownloadPanel


class LidarBCPlugin:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "&LidarBC Downloader"
        self.toolbar = self.iface.addToolBar("LidarBC Downloader")
        self.toolbar.setObjectName("LidarBCDownloaderToolbar")
        self.panel = None
        self.action = None

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(
            icon,
            "LidarBC Batch Downloader",
            self.iface.mainWindow(),
        )
        self.action.triggered.connect(self.run)
        self.action.setEnabled(True)
        self.action.setCheckable(True)

        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)
        self.actions.append(self.action)

    def unload(self):
        """Remove the plugin menu item and icon from QGIS GUI."""
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.toolbar.removeAction(action)
        if self.panel is not None:
            self.iface.removeDockWidget(self.panel)
            self.panel.cleanup()
            self.panel = None
        del self.toolbar

    def run(self):
        """Show or hide the download panel."""
        first_open = self.panel is None
        if self.panel is None:
            self.panel = DownloadPanel(self.iface, self)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.panel)
            self.panel.visibilityChanged.connect(self._on_panel_visibility_changed)
            # Auto-load the LiDARProject_Data_Extent reference layer on first open
            self.panel.load_reference_layer()
        else:
            self.panel.setVisible(not self.panel.isVisible())

        if self.panel.isVisible():
            self.action.setChecked(True)
        else:
            self.action.setChecked(False)

        # Show the welcome popup on first open (or every open until the user
        # ticks "Don't show this again"). It pops up after the panel is set
        # up so it lands centered over the QGIS window with the panel visible
        # behind it.
        if first_open:
            from .welcome_dialog import should_show_welcome, show_welcome_dialog
            if should_show_welcome():
                show_welcome_dialog(self.iface.mainWindow())

    def _on_panel_visibility_changed(self, visible):
        """Keep the toolbar button's check state synced with the panel."""
        if self.action is not None:
            self.action.setChecked(visible)
