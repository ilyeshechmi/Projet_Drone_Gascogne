"""Téléchargement, indexation et sélection des cadastres départementaux."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
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


ESPACE_LIBRE_MINIMUM = 3_000_000_000
VERSION_SCHEMA = "1"
VERSION_CONTROLE_GEOMETRIQUE = "1"
MAX_PARCELLES_SELECTIONNEES = 50
MAX_SOMMETS_CONTOUR = 5_000
Progression = Callable[[str, int], None]


class ErreurCadastre(RuntimeError):
    """Les données cadastrales ne peuvent pas être installées ou interrogées."""


@dataclass(frozen=True)
class ConfigurationCadastre:
    code: str
    nom: str
    taille_archive_estimee: int
    nombre_minimum_parcelles: int

    @property
    def libelle(self) -> str:
        return f"{self.nom} ({self.code})"

    @property
    def url(self) -> str:
        return (
            "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/"
            f"departements/{self.code}/cadastre-{self.code}-parcelles.json.gz"
        )

    @property
    def nom_archive(self) -> str:
        return f"cadastre-{self.code}-parcelles.json.gz"

    @property
    def nom_base(self) -> str:
        return f"cadastre_{self.nom.casefold()}.sqlite"


CADASTRE_GIRONDE = ConfigurationCadastre(
    code="33",
    nom="Gironde",
    taille_archive_estimee=235_520_575,
    nombre_minimum_parcelles=100_000,

)


CADASTRE_LANDES = ConfigurationCadastre(
    code="40",
    nom="Landes",
    taille_archive_estimee=133_837_160,
    nombre_minimum_parcelles=100_000,
)
CONFIGURATIONS_CADASTRALES = (CADASTRE_GIRONDE, CADASTRE_LANDES)



# Alias conservés pour les utilisateurs de l'ancienne API Gironde.
URL_PARCELLES_GIRONDE = CADASTRE_GIRONDE.url
URL_PARCELLES_LANDES = CADASTRE_LANDES.url
TAILLE_TELECHARGEMENT_ESTIMEE = CADASTRE_GIRONDE.taille_archive_estimee
NOMBRE_MINIMUM_PARCELLES_GIRONDE = CADASTRE_GIRONDE.nombre_minimum_parcelles


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
    code_departement: str = ""
    nom_departement: str = ""


    @property
    def cle(self) -> str:
        return f"{self.code_departement}:{self.identifiant}"

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
        departement = (
            f"{self.nom_departement} ({self.code_departement}) — "
            if self.code_departement and self.nom_departement
            else ""
        )
        return f"{departement}{self.reference} — {surface} m²"

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


def _geometrie_exploitable(geometrie) -> bool:
    min_lon, min_lat, max_lon, max_lat = geometrie.bounds
    return (
        all(math.isfinite(value) for value in geometrie.bounds)
        and min_lon < max_lon
        and min_lat < max_lat
        and math.isfinite(float(geometrie.area))
        and geometrie.area > 0
    )


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
    if not path.is_file() or path.stat().st_size < 1_000_000:
        return False
    try:
        with gzip.open(path, "rb") as archive:
            return archive.read(1) in {b"{", b"["}
    except (EOFError, OSError, gzip.BadGzipFile):
        return False


def _verifier_espace_disque(repertoire: Path, nom_departement: str) -> None:
    if shutil.disk_usage(repertoire).free < ESPACE_LIBRE_MINIMUM:
        raise ErreurCadastre(
            f"Espace disque insuffisant pour installer le cadastre de {nom_departement}. "
            "Libérez au moins 3 Go."
        )


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
                "Une autre instance installe déjà des données cadastrales."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(verrou.fileno(), fcntl.LOCK_UN)


def telecharger_archive(
    destination: Path,
    *,
    configuration: ConfigurationCadastre = CADASTRE_GIRONDE,
    url: str | None = None,
    progression: Progression | None = None,
) -> tuple[str, str]:
    """Télécharge l'archive dans un fichier temporaire puis la valide."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _verifier_espace_disque(destination.parent, configuration.nom)
    url = url or configuration.url
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
                reference = taille or configuration.taille_archive_estimee
                _emettre(
                    progression,
                    f"Téléchargement {configuration.nom} : {telecharge / 1_000_000:.0f} Mo",
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
        raise ErreurCadastre(
            f"Impossible de télécharger le cadastre de {configuration.nom} : {exc}"
        ) from exc
    except BaseException:
        temporaire.unlink(missing_ok=True)
        raise




def construire_base_depuis_archive(
    archive: Path,
    destination: Path,
    *,
    configuration: ConfigurationCadastre = CADASTRE_GIRONDE,
    url_source: str | None = None,
    sha256_archive: str = "",
    progression: Progression | None = None,
    nombre_minimum: int = 1,
) -> int:
    """Convertit le GeoJSON compressé en SQLite sans le charger en mémoire."""
    url_source = url_source or configuration.url
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
    geometries_rejetees = 0
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
                commune = str(proprietes.get("commune") or "").strip()
                geometrie_source = feature.get("geometry")
                if not identifiant or not geometrie_source:
                    continue
                if commune and not commune.startswith(configuration.code):
                    raise ErreurCadastre(
                        f"L'archive contient la commune {commune}, incompatible avec "
                        f"le département {configuration.libelle}."
                    )
                geometrie = shape(geometrie_source)
                if geometrie.is_empty or geometrie.geom_type not in {"Polygon", "MultiPolygon"}:
                    continue
                if not _geometrie_exploitable(geometrie):
                    geometries_rejetees += 1
                    continue
                min_lon, min_lat, max_lon, max_lat = geometrie.bounds
                nombre += 1
                lot_parcelles.append(
                    (
                        nombre,
                        identifiant,
                        commune,
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
        if geometries_rejetees and (
            nombre == 0 or geometries_rejetees > max(100, nombre // 1000)
        ):
            raise ErreurCadastre(
                f"Indexation refusée : {geometries_rejetees:,} géométries cadastrales "
                "sont aplaties ou ont perdu la précision de leurs coordonnées."
                .replace(",", " ")
            )
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
                ("version_controle_geometrique", VERSION_CONTROLE_GEOMETRIQUE),
                ("code_departement", configuration.code),
                ("departement", configuration.libelle),
                ("version_donnees", version_donnees),
                ("url_source", url_source),
                ("sha256_archive", sha256_archive),
                ("nombre_parcelles", str(nombre)),
                ("geometries_rejetees", str(geometries_rejetees)),
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
            indexees_valides = verification.execute(
                "SELECT COUNT(*) FROM index_spatial "
                "WHERE min_longitude < max_longitude AND min_latitude < max_latitude"
            ).fetchone()[0]
            schema = verification.execute(
                "SELECT valeur FROM metadonnees WHERE cle='version_schema'"
            ).fetchone()
            echantillon_wkb = verification.execute(
                "SELECT geometrie FROM parcelles ORDER BY id LIMIT 3"
            ).fetchall()
        if (
            integrite != "ok"
            or parcelles != nombre
            or indexees != nombre
            or indexees_valides != nombre
        ):
            raise ErreurCadastre(
                "La base cadastrale construite n'a pas passé le contrôle d'intégrité "
                "des géométries et de l'index spatial."
            )
        if not schema or schema[0] != VERSION_SCHEMA:
            raise ErreurCadastre("La version de la base cadastrale est invalide.")
        if len(echantillon_wkb) != min(3, nombre) or any(
            not _geometrie_exploitable(_loads(bytes(ligne[0])))
            for ligne in echantillon_wkb
        ):
            raise ErreurCadastre(
                "La précision des géométries cadastrales a été perdue pendant "
                "l'écriture de la base."
            )
        _synchroniser_fichier(temporaire)
        os.replace(temporaire, destination)
        _synchroniser_repertoire(destination.parent)
        _emettre(progression, f"Base prête : {nombre:,} parcelles".replace(",", " "), 100)
        return nombre
    except BaseException:
        connexion.close()
        temporaire.unlink(missing_ok=True)
        raise








class CadastreDepartement:
    """Accès en lecture à une base cadastrale locale départementale."""

    def __init__(
        self,
        configuration: ConfigurationCadastre,
        repertoire: Path | None = None,
    ) -> None:
        self.configuration = configuration
        self.repertoire = (repertoire or repertoire_cadastre_par_defaut()).expanduser().resolve()
        self.base = self.repertoire / configuration.nom_base

    @property
    def est_installe(self) -> bool:
        if not self.base.is_file():
            return False
        try:
            informations = self.informations()
        except (OSError, sqlite3.DatabaseError):
            return False
        if informations.get("version_schema") != VERSION_SCHEMA:
            return False
        code = informations.get("code_departement")
        controle = informations.get("version_controle_geometrique")
        if controle != VERSION_CONTROLE_GEOMETRIQUE:
            return (
                code is None
                and self.configuration.code == CADASTRE_GIRONDE.code
                and informations.get("departement") == CADASTRE_GIRONDE.libelle
            )
        if code is not None:
            return code == self.configuration.code
        return informations.get("departement") == self.configuration.libelle

    def informations(self) -> dict[str, str]:
        if not self.base.is_file():
            return {}
        with closing(sqlite3.connect(self.base)) as connexion:
            return dict(connexion.execute("SELECT cle, valeur FROM metadonnees"))

    def installer(self, progression: Progression | None = None) -> int:
        self.repertoire.mkdir(parents=True, exist_ok=True)
        with _verrou_installation(self.repertoire / "installation.lock"):
            _verifier_espace_disque(self.repertoire, self.configuration.nom)
            for temporaire in self.repertoire.glob(f".{self.base.name}.*.part"):
                temporaire.unlink(missing_ok=True)
            archive = self.repertoire / self.configuration.nom_archive
            if _archive_reutilisable(archive):
                _emettre(
                    progression,
                    "Archive complète déjà présente, reprise de l'indexation.",
                    45,
                )
                url_finale = _resoudre_url_source(self.configuration.url)
                empreinte = _sha256_fichier(archive)
            else:
                archive.unlink(missing_ok=True)
                url_finale, empreinte = telecharger_archive(
                    archive,
                    configuration=self.configuration,
                    progression=progression,
                )
            try:
                resultat = construire_base_depuis_archive(
                    archive,
                    self.base,
                    configuration=self.configuration,
                    url_source=url_finale,
                    sha256_archive=empreinte,
                    progression=progression,
                    nombre_minimum=self.configuration.nombre_minimum_parcelles,
                )
            except BaseException:
                archive.unlink(missing_ok=True)
                raise
            archive.unlink(missing_ok=True)
            return resultat

    def _connexion(self) -> sqlite3.Connection:
        if not self.est_installe:
            raise ErreurCadastre(
                f"Les données cadastrales de {self.configuration.nom} ne sont pas installées. "
                "Cliquez sur « Installer les données cadastrales »."
            )
        connexion = sqlite3.connect(f"file:{self.base}?mode=ro", uri=True)
        connexion.row_factory = sqlite3.Row
        return connexion

    def _parcelle(self, ligne: sqlite3.Row) -> ParcelleCadastrale:
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
            code_departement=self.configuration.code,
            nom_departement=self.configuration.nom,
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
        if not commune.startswith(self.configuration.code):
            raise ErreurCadastre(
                f"La commune {commune} n'appartient pas au département "
                f"{self.configuration.libelle}."
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

    def trouver_point(
        self, latitude: float, longitude: float
    ) -> ParcelleCadastrale | None:
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
            return None
        if len(retenues) > 1:
            raise ErreurCadastre(
                "Le clic se trouve sur une limite cadastrale. Cliquez davantage au centre de la parcelle."
            )
        return self._parcelle(retenues[0])

    def rechercher_point(self, latitude: float, longitude: float) -> ParcelleCadastrale:
        parcelle = self.trouver_point(latitude, longitude)
        if parcelle is None:
            raise ErreurCadastre("Aucune parcelle cadastrale n'a été trouvée à cet endroit.")
        return parcelle


class CadastreGironde(CadastreDepartement):
    def __init__(self, repertoire: Path | None = None) -> None:
        super().__init__(CADASTRE_GIRONDE, repertoire)


class CadastreLandes(CadastreDepartement):
    def __init__(self, repertoire: Path | None = None) -> None:
        super().__init__(CADASTRE_LANDES, repertoire)


class CadastresRegionaux:
    """Route automatiquement les recherches vers les cadastres installés."""

    def __init__(
        self,
        cadastres: tuple[CadastreDepartement, ...] | None = None,
    ) -> None:
        self.cadastres = cadastres or (CadastreGironde(), CadastreLandes())

    @property
    def installes(self) -> tuple[CadastreDepartement, ...]:
        return tuple(cadastre for cadastre in self.cadastres if cadastre.est_installe)

    def cadastre_pour_commune(self, commune: str) -> CadastreDepartement:
        commune = commune.strip()
        cadastre = next(
            (
                item
                for item in self.cadastres
                if commune.startswith(item.configuration.code)
            ),
            None,
        )
        if cadastre is None:
            codes = ", ".join(item.configuration.code for item in self.cadastres)
            raise ErreurCadastre(
                f"Le code commune {commune or 'vide'} n'appartient pas aux "
                f"départements pris en charge ({codes})."
            )
        if not cadastre.est_installe:
            raise ErreurCadastre(
                f"Le code commune {commune} appartient à {cadastre.configuration.libelle}. "
                f"Installez d'abord les données cadastrales de {cadastre.configuration.nom}."
            )
        return cadastre

    def rechercher_reference(
        self,
        commune: str,
        section: str,
        numero: str,
        prefixe: str = "",
    ) -> ParcelleCadastrale:
        return self.cadastre_pour_commune(commune).rechercher_reference(
            commune,
            section,
            numero,
            prefixe,
        )

    def rechercher_point(self, latitude: float, longitude: float) -> ParcelleCadastrale:
        installes = self.installes
        if not installes:
            raise ErreurCadastre(
                "Installez les données cadastrales de Gironde ou des Landes avant "
                "de sélectionner une parcelle."
            )
        trouvees = [
            parcelle
            for cadastre in installes
            if (parcelle := cadastre.trouver_point(latitude, longitude)) is not None
        ]
        if not trouvees:
            raise ErreurCadastre(
                "Aucune parcelle n'a été trouvée dans les cadastres installés à cet endroit."
            )
        if len(trouvees) > 1:
            raise ErreurCadastre(
                "Le clic correspond à plusieurs départements. Cliquez davantage au "
                "centre de la parcelle."
            )
        return trouvees[0]


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
                "id": parcelle.cle,
                "properties": {
                    "référence": parcelle.reference,
                    "contenance_m²": parcelle.contenance,
                    "département": parcelle.code_departement,
                },
                "geometry": parcelle.geometrie_geojson(),
            }
            for parcelle in parcelles
        ],
    }
