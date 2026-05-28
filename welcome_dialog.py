# -*- coding: utf-8 -*-
"""
Welcome popup shown the first time the user opens the plugin.

The user can tick "Don't show this again" to suppress future popups; the
choice is persisted via QSettings under the same group as the plugin's
other preferences.
"""

import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


SETTINGS_GROUP = "LidarBCDownloader"
SETTING_HIDE_WELCOME = f"{SETTINGS_GROUP}/hide_welcome"


WELCOME_TITLE = "Welcome to the LidarBC Batch Downloader"

# Main body (rich text). QLabel renders this with setTextFormat(Qt.RichText).
# The Lidar BC link is clickable because we call setOpenExternalLinks(True).
WELCOME_BODY = """
<p>This plugin provides a convenient way to batch-download Lidar point
clouds, DEMs, and DSMs directly from the Government of British Columbia
LidarBC Open Data Portal.</p>

<p><b>How to use it:</b></p>

<ul>
  <li><b>Draw an AOI</b> — click <i>Draw AOI polygon</i>, then left-click on
  the map to add vertices. Right-click (or press Enter) to finish. You can
  draw more than one area if needed.</li>
  <li><b>Pick products</b> — select the Lidar, DEM, and DSM scales you want.</li>
  <li><b>Filter (optional)</b> — keep only the latest tile version, restrict
  by year, or limit to current LidarBC Program files.</li>
  <li><b>Choose an output folder</b> and click <i>Find tiles in AOI</i> to
  preview the count, then <i>Download</i>.</li>
</ul>

<p>Use this link to visit the LiDAR BC website:
<a href="https://lidar.gov.bc.ca/">https://lidar.gov.bc.ca/</a></p>

<p>Files are organized by product, project, and scale. Lidar downloads
include the project metadata PDF automatically.</p>

<p>The six LidarBC index layers have been added to your map and are
turned off by default to improve performance. Enable them in the
<i>Layers</i> panel to view tile boundaries and attributes.</p>
"""

# Disclaimer block rendered in smaller, gray "fine print" at the bottom.
# Links remain clickable.
DISCLAIMER_HTML = """
<p><b>Disclaimer</b></p>

<p>Although every effort has been made to provide accurate information and
locations, the Government of British Columbia makes no representation or
warranties regarding the accuracy of information from this plugin, nor
will it accept responsibility for errors or omissions. Access to and/or
content of this plugin may be suspended, discontinued, or altered, in
part or in whole, at any time, for any reason, with or without prior
notice, at the discretion of the Government of British Columbia.</p>

<p><a href="http://www2.gov.bc.ca/gov/content/home/copyright">&copy;Province of British Columbia</a>,
Ministry of Water, Land and Resource Stewardship.</p>

<p>A <a href="https://www2.gov.bc.ca/gov/content/data/about-data-management/geobc"><b>GeoBC</b></a>
Production.</p>
"""


def should_show_welcome():
    """Return True if the welcome popup hasn't been suppressed by the user."""
    return not QSettings().value(SETTING_HIDE_WELCOME, False, type=bool)


def show_welcome_dialog(parent=None):
    """Display the welcome dialog. If the user ticks 'Don't show again',
    the preference is persisted so future launches skip this.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(WELCOME_TITLE)
    # Wider than default to give the body room and accommodate the logo banner
    dlg.setMinimumWidth(560)

    # The whole content area sits inside a scroll area so a short screen
    # (small laptop, projector) doesn't truncate the disclaimer or the OK
    # button. The dialog itself has a sensible max height.
    dlg.setMaximumHeight(900)

    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(0, 0, 0, 0)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    outer.addWidget(scroll)

    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setSpacing(10)

    # --- Logo banner at the top ---
    # The logo file lives next to this script. Load defensively: if the file
    # is missing or fails to render, the dialog still works without it.
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    if os.path.exists(logo_path):
        pixmap = QPixmap(logo_path)
        if not pixmap.isNull():
            # Scale to ~380px wide while preserving aspect ratio. The source
            # is 833x385, so this gives a ~175px-tall banner that fits nicely
            # above the body without dominating it.
            scaled = pixmap.scaledToWidth(380, Qt.SmoothTransformation)
            logo_label = QLabel()
            logo_label.setPixmap(scaled)
            logo_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(logo_label)

    # --- Body text ---
    body_label = QLabel(WELCOME_BODY)
    body_label.setTextFormat(Qt.RichText)
    body_label.setWordWrap(True)
    body_label.setOpenExternalLinks(True)
    layout.addWidget(body_label)

    # --- Disclaimer (smaller gray "fine print") ---
    disclaimer_label = QLabel(DISCLAIMER_HTML)
    disclaimer_label.setTextFormat(Qt.RichText)
    disclaimer_label.setWordWrap(True)
    disclaimer_label.setOpenExternalLinks(True)
    # Smaller font + muted color → reads as legal fine print without
    # dominating the dialog. The style is applied to the label rather than
    # baked into the HTML so it inherits the user's QGIS theme.
    disclaimer_label.setStyleSheet(
        "QLabel { color: gray; font-size: 9pt; }"
    )
    layout.addWidget(disclaimer_label)

    scroll.setWidget(container)

    # --- "Don't show again" + OK button (outside the scroll area so they
    # never get scrolled off-screen) ---
    bottom_widget = QWidget()
    bottom_layout = QVBoxLayout(bottom_widget)
    bottom_layout.setContentsMargins(10, 5, 10, 10)

    cb_hide = QCheckBox("Don't show this again")
    cb_hide.setChecked(False)
    bottom_layout.addWidget(cb_hide)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.accepted.connect(dlg.accept)
    bottom_layout.addWidget(buttons)

    outer.addWidget(bottom_widget)

    dlg.exec_()

    if cb_hide.isChecked():
        QSettings().setValue(SETTING_HIDE_WELCOME, True)
