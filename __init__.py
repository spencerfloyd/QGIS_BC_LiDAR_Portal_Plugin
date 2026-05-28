# -*- coding: utf-8 -*-
"""
LidarBC Batch Downloader QGIS Plugin
Entry point: returns plugin instance to QGIS.
"""


def classFactory(iface):
    """Load LidarBCPlugin class.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    from .lidarbc_plugin import LidarBCPlugin
    return LidarBCPlugin(iface)
