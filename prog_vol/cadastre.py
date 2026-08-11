"""Téléchargement, indexation et sélection du cadastre officiel de Gironde."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


URL_PARCELLES_GIRONDE = (
    "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/"
    "departements/33/cadastre-33-parcelles.json.gz"
)
TAILLE_TELECHARGEMENT_ESTIMEE = 235_520_575
ESPACE_LIBRE_MINIMUM = 3_000_000_000
VERSION_SCHEMA = "1"
NOMBRE_MINIMUM_PARCELLES_GIRONDE = 100_000
MAX_PARCELLES_SELECTIONNEES = 50
MAX_SOMMETS_CONTOUR = 5_000
Progression = Callable[[str, int], None]


class ErreurCadastre(RuntimeError):
    """Les données cadastrales ne peuvent pas être installées ou interrogées."""


def repertoire_cadastre_par_defaut() -> Path:
    surcharge = os.environ.get("PROG_VOL_CADASTRE_DIR")
    if surcharge:
        return Path(surcharge).expanduser().resolve()
    if sys.platform == "darwin":
        racine = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        racine = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        racine = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return racine / "Projet Drone Gascogne" / "cadastre"


def _dependances_geometriques():
    try:
        import ijson
        from shapely.geometry import Point, mapping, shape
        from shapely.ops import unary_union
        from shapely.validation import make_valid
        from shapely.wkb import dumps, loads
    except ImportError as exc:
        raise ErreurCadastre(
            "Les bibliothèques ijson et shapely sont nécessaires. "
            "Installez les dépendances de prog_vol/requirements.txt."
        ) from exc
    return ijson, Point, mapping, shape, unary_union, make_valid, dumps, loads


@dataclass(frozen=True)
class ParcelleCadastrale:
    identifiant: str
    commune: str
    prefixe: str
    section: str
    numero: str
    contenance: int
    arpentee: bool
    date_mise_a_jour: str
    geometrie_wkb: bytes

    @property
    def reference(self) -> str:
        prefixe = f", préfixe {self.prefixe}" if self.prefixe not in {"", "000"} else ""
        return (
            f"Commune {self.commune}{prefixe}, section {self.section}, "
            f"parcelle n° {self.numero}"
        )

    @property
    def description(self) -> str:
        surface = f"{self.contenance:,}".replace(",", " ")
        return f"{self.reference} — {surface} m²"

    def geometrie_geojson(self) -> dict:
        _ijson, _Point, mapping, _shape, _union, _valid, _dumps, loads = (
            _dependances_geometriques()
        )
        return mapping(loads(self.geometrie_wkb))


@dataclass(frozen=True)
class ResultatFusion:
    points: tuple[tuple[float, float], ...]
    geometrie_geojson: dict
    surface_cadastrale: int


def _normaliser_numero(numero: str) -> str:
    nettoye = numero.strip()
    if not nettoye:
        return ""
    return nettoye.lstrip("0") or "0"


def _emettre(progression: Progression | None, message: str, pourcentage: int) -> None:
    if progression:
        progression(message, max(0, min(100, pourcentage)))


def _chemin_temporaire(repertoire: Path, prefixe: str, suffixe: str) -> Path:
    descripteur, chemin = tempfile.mkstemp(dir=repertoire, prefix=prefixe, suffix=suffixe)
    os.close(descripteur)
    temporaire = Path(chemin)
    temporaire.unlink()
    return temporaire


def _synchroniser_fichier(path: Path) -> None:
    with path.open("rb") as flux:
        os.fsync(flux.fileno())


def _synchroniser_repertoire(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descripteur = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descripteur)
    finally:
        os.close(descripteur)


def _sha256_fichier(path: Path) -> str:
    empreinte = hashlib.sha256()
    with path.open("rb") as flux:
        while bloc := flux.read(1024 * 1024):
            empreinte.update(bloc)
    return empreinte.hexdigest()


def _archive_reutilisable(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != TAILLE_TELECHARGEMENT_ESTIMEE:
        return False
    try:
        with gzip.open(path, "rb") as archive:
            return archive.read(1) in {b"{", b"["}
    except (EOFError, OSError, gzip.BadGzipFile):
        return False


def _resoudre_url_source(url: str) -> str:
    try:
        requete = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "Projet-Drone-Gascogne/1.0"},
        )
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            return reponse.geturl()
    except (OSError, urllib.error.URLError):
        return url


@contextmanager
def _verrou_installation(path: Path):
    try:
        import fcntl
    except ImportError as exc:
        raise ErreurCadastre("Le verrou d'installation cadastrale n'est pas disponible.") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as verrou:
        try:
            fcntl.flock(verrou.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ErreurCadastre(
                "Une autre instance installe déjà les données cadastrales de Gironde."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(verrou.fileno(), fcntl.LOCK_UN)


def telecharger_archive(
    destination: Path,
    *,
    url: str = URL_PARCELLES_GIRONDE,
    progression: Progression | None = None,
) -> tuple[str, str]:
    """Télécharge l'archive dans un fichier temporaire puis la valide."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    espace_libre = shutil.disk_usage(destination.parent).free
    if espace_libre < ESPACE_LIBRE_MINIMUM:
        raise ErreurCadastre(
            "Espace disque insuffisant pour installer le cadastre de Gironde. "
            "Libérez au moins 3 Go."
        )
    temporaire = _chemin_temporaire(
        destination.parent,
        f".{destination.name}.",
        ".part",
    )
    requete = urllib.request.Request(
        url,
        headers={"User-Agent": "Projet-Drone-Gascogne/1.0"},
    )
    empreinte = hashlib.sha256()
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse, temporaire.open("wb") as sortie:
            taille = int(reponse.headers.get("Content-Length") or 0)
            telecharge = 0
            while bloc := reponse.read(1024 * 1024):
                sortie.write(bloc)
                empreinte.update(bloc)
                telecharge += len(bloc)
                reference = taille or TAILLE_TELECHARGEMENT_ESTIMEE
                _emettre(
                    progression,
                    f"Téléchargement du cadastre : {telecharge / 1_000_000:.0f} Mo",
                    int(45 * telecharge / reference),
                )
            sortie.flush()
            os.fsync(sortie.fileno())
            url_finale = reponse.geturl()
        if taille and telecharge != taille:
            raise ErreurCadastre(
                f"Téléchargement incomplet : {telecharge} octets reçus sur {taille}."
            )
        if temporaire.stat().st_size < 1_000_000:
            raise ErreurCadastre("L'archive cadastrale téléchargée est anormalement petite.")
        try:
            with gzip.open(temporaire, "rb") as archive:
                if archive.read(1) not in {b"{", b"["}:
                    raise ErreurCadastre(
                        "L'archive cadastrale ne contient pas de GeoJSON valide."
                    )
        except (EOFError, gzip.BadGzipFile) as exc:
            raise ErreurCadastre("L'archive cadastrale téléchargée est corrompue.") from exc
        os.replace(temporaire, destination)
        return url_finale, empreinte.hexdigest()
    except (OSError, urllib.error.URLError) as exc:
        temporaire.unlink(missing_ok=True)
        if isinstance(exc, ErreurCadastre):
            raise
        raise ErreurCadastre(f"Impossible de télécharger le cadastre de Gironde : {exc}") from exc
    except BaseException:
        temporaire.unlink(missing_ok=True)
        raise


def construire_base_depuis_archive(
    archive: Path,
    destination: Path,
    *,
    url_source: str = URL_PARCELLES_GIRONDE,
    sha256_archive: str = "",
    progression: Progression | None = None,
    nombre_minimum: int = 1,
) -> int:
    """Convertit le GeoJSON compressé en SQLite sans le charger en mémoire."""
    ijson, _Point, _mapping, shape, _union, make_valid, dumps, _loads = (
        _dependances_geometriques()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporaire = _chemin_temporaire(
        destination.parent,
        f".{destination.name}.",
        ".part",
    )
    connexion = sqlite3.connect(temporaire)
    nombre = 0
    try:
        connexion.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            PRAGMA locking_mode=EXCLUSIVE;
            PRAGMA page_size=32768;
            PRAGMA cache_size=-200000;
            CREATE TABLE metadonnees (
                cle TEXT PRIMARY KEY,
                valeur TEXT NOT NULL
            );
            CREATE TABLE parcelles (
                id INTEGER PRIMARY KEY,
                identifiant TEXT NOT NULL UNIQUE,
                commune TEXT NOT NULL,
                prefixe TEXT NOT NULL,
                section TEXT NOT NULL,
                numero TEXT NOT NULL,
                contenance INTEGER NOT NULL,
                arpentee INTEGER NOT NULL,
                date_creation TEXT NOT NULL,
                date_mise_a_jour TEXT NOT NULL,
                geometrie BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE index_spatial USING rtree(
                id, min_longitude, max_longitude, min_latitude, max_latitude
            );
            """
        )
        lot_parcelles: list[tuple] = []
        lot_spatial: list[tuple] = []

        def enregistrer_lot() -> None:
            if not lot_parcelles:
                return
            connexion.executemany(
                """
                INSERT INTO parcelles(
                    id, identifiant, commune, prefixe, section, numero, contenance,
                    arpentee, date_creation, date_mise_a_jour, geometrie
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                lot_parcelles,
            )
            connexion.executemany(
                "INSERT INTO index_spatial VALUES (?, ?, ?, ?, ?)",
                lot_spatial,
            )
            connexion.commit()
            lot_parcelles.clear()
            lot_spatial.clear()

        with gzip.open(archive, "rb") as flux:
            for feature in ijson.items(flux, "features.item", use_float=True):
                proprietes = feature.get("properties") or {}
                identifiant = str(proprietes.get("id") or feature.get("id") or "").strip()
                geometrie_source = feature.get("geometry")
                if not identifiant or not geometrie_source:
                    continue
                geometrie = shape(geometrie_source)
                if geometrie.is_empty or geometrie.geom_type not in {"Polygon", "MultiPolygon"}:
                    continue
                min_lon, min_lat, max_lon, max_lat = geometrie.bounds
                nombre += 1
                lot_parcelles.append(
                    (
                        nombre,
                        identifiant,
                        str(proprietes.get("commune") or ""),
                        str(proprietes.get("prefixe") or "000"),
                        str(proprietes.get("section") or "").upper(),
                        _normaliser_numero(str(proprietes.get("numero") or "")),
                        int(proprietes.get("contenance") or 0),
                        int(bool(proprietes.get("arpente"))),
                        str(proprietes.get("created") or ""),
                        str(proprietes.get("updated") or ""),
                        sqlite3.Binary(dumps(geometrie)),
                    )
                )
                lot_spatial.append(
                    (nombre, min_lon, max_lon, min_lat, max_lat)
                )
                if len(lot_parcelles) >= 10_000:
                    enregistrer_lot()
                    _emettre(
                        progression,
                        f"Indexation : {nombre:,} parcelles".replace(",", " "),
                        min(98, 45 + nombre // 30_000),
                    )
        enregistrer_lot()
        if nombre < nombre_minimum:
            raise ErreurCadastre(
                f"Base cadastrale incomplète : {nombre:,} parcelles valides, "
                f"au moins {nombre_minimum:,} attendues.".replace(",", " ")
            )
        version_donnees = "inconnue"
        marqueur = "/etalab-cadastre/"
        if marqueur in url_source:
            version_donnees = url_source.split(marqueur, 1)[1].split("/", 1)[0]
        connexion.executemany(
            "INSERT INTO metadonnees(cle, valeur) VALUES (?, ?)",
            (
                ("version_schema", VERSION_SCHEMA),
                ("departement", "Gironde (33)"),
                ("version_donnees", version_donnees),
                ("url_source", url_source),
                ("sha256_archive", sha256_archive),
                ("nombre_parcelles", str(nombre)),
            ),
        )
        connexion.execute(
            "CREATE INDEX recherche_reference "
            "ON parcelles(commune, prefixe, section, numero)"
        )
        connexion.commit()
        connexion.execute("PRAGMA optimize")
        connexion.close()
        with closing(sqlite3.connect(f"file:{temporaire}?mode=ro", uri=True)) as verification:
            integrite = verification.execute("PRAGMA quick_check").fetchone()[0]
            parcelles = verification.execute("SELECT COUNT(*) FROM parcelles").fetchone()[0]
            indexees = verification.execute("SELECT COUNT(*) FROM index_spatial").fetchone()[0]
            schema = verification.execute(
                "SELECT valeur FROM metadonnees WHERE cle='version_schema'"
            ).fetchone()
        if integrite != "ok" or parcelles != nombre or indexees != nombre:
            raise ErreurCadastre(
                "La base cadastrale construite n'a pas passé le contrôle d'intégrité."
            )
        if not schema or schema[0] != VERSION_SCHEMA:
            raise ErreurCadastre("La version de la base cadastrale est invalide.")
        _synchroniser_fichier(temporaire)
        os.replace(temporaire, destination)
        _synchroniser_repertoire(destination.parent)
        _emettre(progression, f"Base prête : {nombre:,} parcelles".replace(",", " "), 100)
        return nombre
    except BaseException:
        connexion.close()
        temporaire.unlink(missing_ok=True)
        raise


class CadastreGironde:
    """Accès en lecture à la base cadastrale locale et gestion de son installation."""

    def __init__(self, repertoire: Path | None = None) -> None:
        self.repertoire = (repertoire or repertoire_cadastre_par_defaut()).expanduser().resolve()
        self.base = self.repertoire / "cadastre_gironde.sqlite"

    @property
    def est_installe(self) -> bool:
        if not self.base.is_file():
            return False
        try:
            informations = self.informations()
        except (OSError, sqlite3.DatabaseError):
            return False
        return informations.get("version_schema") == VERSION_SCHEMA

    def informations(self) -> dict[str, str]:
        if not self.base.is_file():
            return {}
        with closing(sqlite3.connect(self.base)) as connexion:
            return dict(connexion.execute("SELECT cle, valeur FROM metadonnees"))

    def installer(self, progression: Progression | None = None) -> int:
        self.repertoire.mkdir(parents=True, exist_ok=True)
        with _verrou_installation(self.repertoire / "installation.lock"):
            for temporaire in self.repertoire.glob(".cadastre_gironde.sqlite.*.part"):
                temporaire.unlink(missing_ok=True)
            archive = self.repertoire / "cadastre-33-parcelles.json.gz"
            if _archive_reutilisable(archive):
                _emettre(
                    progression,
                    "Archive complète déjà présente, reprise de l'indexation.",
                    45,
                )
                url_finale = _resoudre_url_source(URL_PARCELLES_GIRONDE)
                empreinte = _sha256_fichier(archive)
            else:
                archive.unlink(missing_ok=True)
                url_finale, empreinte = telecharger_archive(archive, progression=progression)
            resultat = construire_base_depuis_archive(
                archive,
                self.base,
                url_source=url_finale,
                sha256_archive=empreinte,
                progression=progression,
                nombre_minimum=NOMBRE_MINIMUM_PARCELLES_GIRONDE,
            )
            archive.unlink(missing_ok=True)
            return resultat

    def _connexion(self) -> sqlite3.Connection:
        if not self.est_installe:
            raise ErreurCadastre(
                "Les données cadastrales de Gironde ne sont pas installées. "
                "Cliquez sur « Installer les données cadastrales »."
            )
        connexion = sqlite3.connect(f"file:{self.base}?mode=ro", uri=True)
        connexion.row_factory = sqlite3.Row
        return connexion

    @staticmethod
    def _parcelle(ligne: sqlite3.Row) -> ParcelleCadastrale:
        return ParcelleCadastrale(
            identifiant=ligne["identifiant"],
            commune=ligne["commune"],
            prefixe=ligne["prefixe"],
            section=ligne["section"],
            numero=ligne["numero"],
            contenance=int(ligne["contenance"]),
            arpentee=bool(ligne["arpentee"]),
            date_mise_a_jour=ligne["date_mise_a_jour"],
            geometrie_wkb=bytes(ligne["geometrie"]),
        )

    def rechercher_reference(
        self,
        commune: str,
        section: str,
        numero: str,
        prefixe: str = "",
    ) -> ParcelleCadastrale:
        commune = commune.strip()
        section = section.strip().upper()
        numero = _normaliser_numero(numero)
        prefixe = prefixe.strip()
        if not commune or not section or not numero:
            raise ErreurCadastre(
                "Renseignez le code INSEE de la commune, la section et le numéro de parcelle."
            )
        requete = "SELECT * FROM parcelles WHERE commune=? AND section=? AND numero=?"
        valeurs: list[str] = [commune, section, numero]
        if prefixe:
            requete += " AND prefixe=?"
            valeurs.append(prefixe.zfill(3))
        with closing(self._connexion()) as connexion:
            lignes = connexion.execute(requete, valeurs).fetchall()
        if not lignes:
            raise ErreurCadastre("Aucune parcelle cadastrale ne correspond à cette référence.")
        if len(lignes) > 1:
            raise ErreurCadastre(
                "Plusieurs parcelles correspondent à cette référence. "
                "Renseignez également le préfixe cadastral."
            )
        return self._parcelle(lignes[0])

    def rechercher_point(self, latitude: float, longitude: float) -> ParcelleCadastrale:
        _ijson, Point, _mapping, _shape, _union, _valid, _dumps, loads = (
            _dependances_geometriques()
        )
        point = Point(float(longitude), float(latitude))
        with closing(self._connexion()) as connexion:
            lignes = connexion.execute(
                """
                SELECT p.* FROM parcelles p
                JOIN index_spatial i ON i.id=p.id
                WHERE i.min_longitude<=? AND i.max_longitude>=?
                  AND i.min_latitude<=? AND i.max_latitude>=?
                """,
                (longitude, longitude, latitude, latitude),
            ).fetchall()
        candidates = [(ligne, loads(bytes(ligne["geometrie"]))) for ligne in lignes]
        strictes = [ligne for ligne, geometrie in candidates if geometrie.contains(point)]
        retenues = strictes or [ligne for ligne, geometrie in candidates if geometrie.covers(point)]
        if not retenues:
            raise ErreurCadastre("Aucune parcelle cadastrale n'a été trouvée à cet endroit.")
        if len(retenues) > 1:
            raise ErreurCadastre(
                "Le clic se trouve sur une limite cadastrale. Cliquez davantage au centre de la parcelle."
            )
        return self._parcelle(retenues[0])


def fusionner_parcelles(parcelles: list[ParcelleCadastrale]) -> ResultatFusion:
    if not parcelles:
        raise ErreurCadastre("Sélectionnez au moins une parcelle cadastrale.")
    if len(parcelles) > MAX_PARCELLES_SELECTIONNEES:
        raise ErreurCadastre(
            f"La sélection est limitée à {MAX_PARCELLES_SELECTIONNEES} parcelles "
            "pour préserver la réactivité de l'application."
        )
    _ijson, _Point, mapping, _shape, unary_union, make_valid, _dumps, loads = (
        _dependances_geometriques()
    )
    geometrie = unary_union([loads(parcelle.geometrie_wkb) for parcelle in parcelles])
    if not geometrie.is_valid:
        geometrie = make_valid(geometrie)
    if geometrie.geom_type != "Polygon":
        raise ErreurCadastre(
            "Les parcelles sélectionnées ne forment pas une zone continue. "
            "Pour cette version, choisissez uniquement des parcelles adjacentes."
        )
    if geometrie.interiors:
        raise ErreurCadastre(
            "La sélection forme un trou intérieur qui n'est pas pris en charge. "
            "Modifiez la sélection cadastrale."
        )
    coordonnees = list(geometrie.exterior.coords)
    if len(coordonnees) > 1 and coordonnees[0] == coordonnees[-1]:
        coordonnees.pop()
    if len(coordonnees) < 3:
        raise ErreurCadastre("Le contour cadastral obtenu est invalide.")
    if len(coordonnees) > MAX_SOMMETS_CONTOUR:
        raise ErreurCadastre(
            f"Le contour fusionné dépasse {MAX_SOMMETS_CONTOUR} sommets. "
            "Réduisez le nombre de parcelles sélectionnées."
        )
    points = tuple((float(latitude), float(longitude)) for longitude, latitude in coordonnees)
    return ResultatFusion(
        points=points,
        geometrie_geojson=mapping(geometrie),
        surface_cadastrale=sum(parcelle.contenance for parcelle in parcelles),
    )


def collection_geojson(parcelles: list[ParcelleCadastrale]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": parcelle.identifiant,
                "properties": {
                    "référence": parcelle.reference,
                    "contenance_m²": parcelle.contenance,
                },
                "geometry": parcelle.geometrie_geojson(),
            }
            for parcelle in parcelles
        ],
    }
