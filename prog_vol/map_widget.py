"""Widget Leaflet utilisé pour dessiner la zone et prévisualiser le vol."""

from __future__ import annotations

import json

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import QVBoxLayout, QWidget


MAP_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Zone de mission</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <style>
    html, body, #map { height: 100%; margin: 0; background: #e8edf0; }
    .leaflet-control-attribution { font-size: 10px; }
    .map-hint {
      position: absolute; z-index: 900; left: 14px; bottom: 22px;
      max-width: 330px; padding: 9px 12px; border-radius: 8px;
      color: #18313d; background: rgba(255,255,255,.94);
      box-shadow: 0 3px 14px rgba(20,42,52,.18);
      font: 600 12px Arial, sans-serif;
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <div id="hint" class="map-hint">Cliquez sur la carte pour placer les sommets.</div>
  <script>
    const map = L.map('map', { zoomControl: true }).setView([44.8378, -0.5792], 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    let bridge = null;
    let points = [];
    let pointMarkers = [];
    let outline = null;
    let waypointLayer = L.layerGroup().addTo(map);
    let drawingClosed = false;

    new QWebChannel(qt.webChannelTransport, function(channel) {
      bridge = channel.objects.bridge;
    });

    function updateHint() {
      const hint = document.getElementById('hint');
      if (drawingClosed) {
        hint.textContent = 'Zone fermée. Générez la mission ou réinitialisez la carte.';
      } else if (points.length < 3) {
        hint.textContent = points.length + ' sommet(s). Ajoutez au moins ' + (3 - points.length) + '.';
      } else {
        hint.textContent = points.length + ' sommets. Vous pouvez fermer le polygone.';
      }
    }

    function redrawOutline(closed) {
      if (outline) map.removeLayer(outline);
      outline = null;
      if (points.length >= 2) {
        outline = closed
          ? L.polygon(points, {color:'#126b82', weight:3, fillColor:'#2f91a8', fillOpacity:.17}).addTo(map)
          : L.polyline(points, {color:'#126b82', weight:3, dashArray:'7 6'}).addTo(map);
      }
    }

    map.on('click', function(event) {
      if (drawingClosed || !bridge) return;
      const point = [event.latlng.lat, event.latlng.lng];
      points.push(point);
      const marker = L.circleMarker(point, {
        radius: 6, color: '#0b596c', fillColor: '#ffffff', fillOpacity: 1, weight: 3
      }).addTo(map).bindTooltip('Sommet ' + points.length);
      pointMarkers.push(marker);
      redrawOutline(false);
      updateHint();
      bridge.addPoint(point[0], point[1]);
    });

    window.kaelMap = {
      setCenter: function(latitude, longitude, zoom) {
        map.setView([latitude, longitude], zoom || 16);
      },
      closePolygon: function() {
        if (points.length < 3) return;
        drawingClosed = true;
        redrawOutline(true);
        if (outline) map.fitBounds(outline.getBounds(), {padding:[35,35]});
        updateHint();
      },
      reset: function() {
        pointMarkers.forEach(marker => map.removeLayer(marker));
        pointMarkers = [];
        points = [];
        drawingClosed = false;
        if (outline) map.removeLayer(outline);
        outline = null;
        waypointLayer.clearLayers();
        updateHint();
      },
      showWaypoints: function(coordinates) {
        waypointLayer.clearLayers();
        if (!coordinates.length) return;
        const path = L.polyline(coordinates, {color:'#d04a37', weight:2.5, opacity:.9}).addTo(waypointLayer);
        coordinates.forEach(function(point, index) {
          L.circleMarker(point, {
            radius: 3, color:'#a82e20', fillColor:'#ef765f', fillOpacity:1, weight:1
          }).addTo(waypointLayer).bindTooltip('WP ' + (index + 1));
        });
        map.fitBounds(path.getBounds(), {padding:[35,35]});
      }
    };
    updateHint();
  </script>
</body>
</html>
"""


class MapBridge(QObject):
    point_added = pyqtSignal(float, float)

    @pyqtSlot(float, float)
    def addPoint(self, latitude: float, longitude: float) -> None:
        self.point_added.emit(latitude, longitude)


class MissionMapWidget(QWidget):
    polygon_changed = pyqtSignal(object)
    polygon_closed_changed = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[float, float]] = []
        self._closed = False
        self._loaded = False
        self._pending_scripts: list[str] = []
        self.view = QWebEngineView(self)
        self.bridge = MapBridge(self)
        self.bridge.point_added.connect(self._point_added)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.loadFinished.connect(self._load_finished)
        self.view.setHtml(MAP_HTML)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._points)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @pyqtSlot(float, float)
    def _point_added(self, latitude: float, longitude: float) -> None:
        if self._closed:
            return
        self._points.append((latitude, longitude))
        self.polygon_changed.emit(self.points)

    def close_polygon(self) -> bool:
        if len(self._points) < 3:
            return False
        self._closed = True
        self._run_script("window.kaelMap.closePolygon();")
        self.polygon_closed_changed.emit(True)
        return True

    def reset(self) -> None:
        self._points.clear()
        self._closed = False
        self._run_script("window.kaelMap.reset();")
        self.polygon_changed.emit(self.points)
        self.polygon_closed_changed.emit(False)

    def set_center(self, latitude: float, longitude: float, zoom: int = 16) -> None:
        script = f"window.kaelMap.setCenter({float(latitude)}, {float(longitude)}, {int(zoom)});"
        self._run_script(script)

    def show_waypoints(self, waypoints: tuple[tuple[float, float, float], ...]) -> None:
        coordinates = [[latitude, longitude] for latitude, longitude, _ in waypoints]
        payload = json.dumps(coordinates, separators=(",", ":"))
        self._run_script(f"window.kaelMap.showWaypoints({payload});")

    @pyqtSlot(bool)
    def _load_finished(self, succeeded: bool) -> None:
        self._loaded = succeeded
        if not succeeded:
            self._pending_scripts.clear()
            return
        pending, self._pending_scripts = self._pending_scripts, []
        for script in pending:
            self.view.page().runJavaScript(script)

    def _run_script(self, script: str) -> None:
        if self._loaded:
            self.view.page().runJavaScript(script)
        else:
            self._pending_scripts.append(script)
