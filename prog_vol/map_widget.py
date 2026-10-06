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
    const fondOpenStreetMap = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);
    const coucheCadastre = L.tileLayer(
      'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
      '&LAYER=CADASTRALPARCELS.PARCELLAIRE_EXPRESS&STYLE=PCI%20vecteur' +
      '&FORMAT=image/png&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
      {
        minZoom: 14,
        maxZoom: 20,
        opacity: 0.78,
        attribution: '&copy; IGN - Parcellaire Express (PCI)'
      }
    );
    L.control.layers(
      {'OpenStreetMap': fondOpenStreetMap},
      {'Parcelles cadastrales officielles': coucheCadastre},
      {collapsed: false}
    ).addTo(map);

    let bridge = null;
    let modeSelection = 'dessin';
    let interactionActive = true;
    let homePlacement = false;
    let homeMarker = null;
    let points = [];
    let pointMarkers = [];
    let outline = null;
    let flightZone = null;
    let waypointLayer = L.layerGroup().addTo(map);
    let parcellesSelectionnees = L.geoJSON(null, {
      style: {color:'#ad5a13', weight:3, fillColor:'#f2a65a', fillOpacity:.27},
      onEachFeature: function(feature, layer) {
        const proprietes = feature.properties || {};
        if (proprietes['référence']) {
          layer.bindTooltip(
            proprietes['référence'] + ' — ' + (proprietes['contenance_m²'] || 0) + ' m²'
          );
        }
      }
    }).addTo(map);
    let drawingClosed = false;

    new QWebChannel(qt.webChannelTransport, function(channel) {
      bridge = channel.objects.bridge;
    });

    function updateHint() {
      const hint = document.getElementById('hint');
      if (homePlacement) {
        hint.textContent = 'Cliquez sur la carte pour placer le point de décollage et de retour.';
        return;
      }
      if (modeSelection === 'cadastre') {
        hint.textContent = 'Mode cadastral : cliquez au centre d’une parcelle de Gironde ou des Landes.';
        return;
      }
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
      if (!bridge || !interactionActive) return;
      if (homePlacement) {
        const point = [event.latlng.lat, event.latlng.lng];
        if (homeMarker) map.removeLayer(homeMarker);
        homeMarker = L.circleMarker(point, {
          radius: 8, color: '#7b3f00', fillColor: '#f4a340', fillOpacity: 1, weight: 3
        }).addTo(map).bindTooltip('Home : décollage et retour');
        homePlacement = false;
        updateHint();
        bridge.setHome(point[0], point[1]);
        return;
      }
      if (modeSelection === 'cadastre') {
        bridge.selectionnerParcelle(event.latlng.lat, event.latlng.lng);
        return;
      }
      if (drawingClosed) return;
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
      setModeSelection: function(mode) {
        modeSelection = mode === 'cadastre' ? 'cadastre' : 'dessin';
        homePlacement = false;
        if (modeSelection === 'cadastre' && !map.hasLayer(coucheCadastre)) {
          coucheCadastre.addTo(map);
        }
        updateHint();
      },
      setInteractionActive: function(active) {
        interactionActive = Boolean(active);
      },
      setHomePlacement: function(active) {
        homePlacement = Boolean(active);
        updateHint();
      },
      clearHome: function() {
        homePlacement = false;
        if (homeMarker) map.removeLayer(homeMarker);
        homeMarker = null;
        updateHint();
      },
      setMissionPolygon: function(coordinates) {
        pointMarkers.forEach(marker => map.removeLayer(marker));
        pointMarkers = [];
        points = coordinates || [];
        drawingClosed = points.length >= 3;
        redrawOutline(drawingClosed);
        if (outline) map.fitBounds(outline.getBounds(), {padding:[35,35]});
        updateHint();
      },
      setFlightZonePolygon: function(coordinates) {
        if (flightZone) map.removeLayer(flightZone);
        flightZone = null;
        if (coordinates && coordinates.length >= 3) {
          flightZone = L.polygon(coordinates, {
            color:'#6f4aa8', weight:2.5, dashArray:'8 7', fill:false
          }).addTo(map).bindTooltip('Zone de vol avec marge');
        }
      },
      clearFlightZone: function() {
        if (flightZone) map.removeLayer(flightZone);
        flightZone = null;
      },
      afficherParcellesSelectionnees: function(collection) {
        parcellesSelectionnees.clearLayers();
        if (collection && collection.features) {
          parcellesSelectionnees.addData(collection);
        }
      },
      reset: function() {
        pointMarkers.forEach(marker => map.removeLayer(marker));
        pointMarkers = [];
        points = [];
        drawingClosed = false;
        if (outline) map.removeLayer(outline);
        outline = null;
        if (flightZone) map.removeLayer(flightZone);
        flightZone = null;
        waypointLayer.clearLayers();
        parcellesSelectionnees.clearLayers();
        if (homeMarker) map.removeLayer(homeMarker);
        homeMarker = null;
        homePlacement = false;
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
      },
      showMissionParts: function(parts) {
        waypointLayer.clearLayers();
        const colors = ['#c43d2f', '#147d72', '#7856a8', '#c17b16', '#2368a2', '#9b3f76'];
        const bounds = [];
        parts.forEach(function(coordinates, partIndex) {
          if (!coordinates.length) return;
          const color = colors[partIndex % colors.length];
          L.polyline(coordinates, {color:color, weight:3, opacity:.92}).addTo(waypointLayer);
          coordinates.forEach(function(point, waypointIndex) {
            bounds.push(point);
            const marker = L.circleMarker(point, {
              radius: waypointIndex === 0 || waypointIndex === coordinates.length - 1 ? 5 : 3,
              color:color, fillColor:color, fillOpacity:1, weight:1
            }).addTo(waypointLayer);
            marker.bindTooltip(
              'Partie ' + (partIndex + 1) + ' — WP ' + (waypointIndex + 1)
              + (waypointIndex === 0 ? ' — début' : '')
              + (waypointIndex === coordinates.length - 1 ? ' — fin' : '')
            );
          });
        });
        if (bounds.length) map.fitBounds(bounds, {padding:[35,35]});
      }
    };
    updateHint();
  </script>
</body>
</html>
"""


class MapBridge(QObject):
    point_added = pyqtSignal(float, float)
    parcelle_demandee = pyqtSignal(float, float)
    home_selected = pyqtSignal(float, float)

    @pyqtSlot(float, float)
    def addPoint(self, latitude: float, longitude: float) -> None:
        self.point_added.emit(latitude, longitude)

    @pyqtSlot(float, float)
    def selectionnerParcelle(self, latitude: float, longitude: float) -> None:
        self.parcelle_demandee.emit(latitude, longitude)

    @pyqtSlot(float, float)
    def setHome(self, latitude: float, longitude: float) -> None:
        self.home_selected.emit(latitude, longitude)


class MissionMapWidget(QWidget):
    polygon_changed = pyqtSignal(object)
    polygon_closed_changed = pyqtSignal(bool)
    parcelle_demandee = pyqtSignal(float, float)
    home_changed = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[float, float]] = []
        self._closed = False
        self._home_point: tuple[float, float] | None = None
        self._loaded = False
        self._pending_scripts: list[str] = []
        self.view = QWebEngineView(self)
        self.bridge = MapBridge(self)
        self.bridge.point_added.connect(self._point_added)
        self.bridge.parcelle_demandee.connect(self.parcelle_demandee)
        self.bridge.home_selected.connect(self._home_selected)
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

    @property
    def home_point(self) -> tuple[float, float] | None:
        return self._home_point

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
        self._home_point = None
        self._run_script("window.kaelMap.reset();")
        self.polygon_changed.emit(self.points)
        self.polygon_closed_changed.emit(False)
        self.home_changed.emit(None)

    def set_center(self, latitude: float, longitude: float, zoom: int = 16) -> None:
        script = f"window.kaelMap.setCenter({float(latitude)}, {float(longitude)}, {int(zoom)});"
        self._run_script(script)

    def set_mode_selection(self, cadastral: bool) -> None:
        mode = "cadastre" if cadastral else "dessin"
        self._run_script(f"window.kaelMap.setModeSelection('{mode}');")

    def begin_home_placement(self) -> None:
        self._run_script("window.kaelMap.setHomePlacement(true);")

    def clear_home(self) -> None:
        self._home_point = None
        self._run_script("window.kaelMap.clearHome();")
        self.home_changed.emit(None)

    @pyqtSlot(float, float)
    def _home_selected(self, latitude: float, longitude: float) -> None:
        self._home_point = (float(latitude), float(longitude))
        self.home_changed.emit(self._home_point)

    def set_interaction_active(self, active: bool) -> None:
        valeur = "true" if active else "false"
        self._run_script(f"window.kaelMap.setInteractionActive({valeur});")

    def set_polygon(self, points: tuple[tuple[float, float], ...]) -> None:
        if len(points) < 3:
            raise ValueError("Un contour doit contenir au moins trois sommets.")
        self._points = [(float(latitude), float(longitude)) for latitude, longitude in points]
        self._closed = True
        payload = json.dumps(self._points, separators=(",", ":"))
        self._run_script(f"window.kaelMap.setMissionPolygon({payload});")
        self.polygon_changed.emit(self.points)
        self.polygon_closed_changed.emit(True)

    def set_flight_zone(self, points: tuple[tuple[float, float], ...]) -> None:
        payload = json.dumps(points, separators=(",", ":"))
        self._run_script(f"window.kaelMap.setFlightZonePolygon({payload});")

    def clear_flight_zone(self) -> None:
        self._run_script("window.kaelMap.clearFlightZone();")

    def afficher_parcelles_selectionnees(self, collection: dict) -> None:
        payload = json.dumps(collection, ensure_ascii=False, separators=(",", ":"))
        self._run_script(f"window.kaelMap.afficherParcellesSelectionnees({payload});")

    def show_waypoints(self, waypoints: tuple[tuple[float, float, float], ...]) -> None:
        coordinates = [[latitude, longitude] for latitude, longitude, _ in waypoints]
        payload = json.dumps(coordinates, separators=(",", ":"))
        self._run_script(f"window.kaelMap.showWaypoints({payload});")

    def show_mission_parts(
        self,
        parts: tuple[tuple[tuple[float, float, float], ...], ...],
    ) -> None:
        coordinates = [
            [[latitude, longitude] for latitude, longitude, _ in waypoints]
            for waypoints in parts
        ]
        payload = json.dumps(coordinates, separators=(",", ":"))
        self._run_script(f"window.kaelMap.showMissionParts({payload});")

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
