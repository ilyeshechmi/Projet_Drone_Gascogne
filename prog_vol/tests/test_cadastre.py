"""Tests de la base cadastrale locale sans téléchargement réseau."""

from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prog_vol.cadastre import (
    CADASTRE_GIRONDE,
    CADASTRE_LANDES,
    CadastreGironde,
    CadastreLandes,
    CadastresRegionaux,
    ErreurCadastre,
    collection_geojson,
    construire_base_depuis_archive,
    fusionner_parcelles,
)
from prog_vol.generator import MissionParameters, generate_waypoints_polygon


def feature(
    identifiant: str,
    numero: str,
    coordonnees: list[list[float]],
    contenance: int,
    commune: str = "33522",
) -> dict:
    return {
        "type": "Feature",
        "id": identifiant,
        "properties": {
            "id": identifiant,
            "commune": commune,
            "prefixe": "000",
            "section": "AB",
            "numero": numero,
            "contenance": contenance,
            "arpente": True,
            "created": "2020-01-01",
            "updated": "2026-06-01",
        },
        "geometry": {"type": "Polygon", "coordinates": [coordonnees]},
    }


FEATURES = [
    feature(
        "33522000AB0001",
        "1",
        [
            [-0.6000, 44.8000],
            [-0.5990, 44.8000],
            [-0.5990, 44.8010],
            [-0.6000, 44.8010],
            [-0.6000, 44.8000],
        ],
        1000,
    ),
    feature(
        "33522000AB0002",
        "2",
        [
            [-0.5990, 44.8000],
            [-0.5980, 44.8000],
            [-0.5980, 44.8010],
            [-0.5990, 44.8010],
            [-0.5990, 44.8000],
        ],
        1200,
    ),
    feature(
        "33522000AB0003",
        "3",
        [
            [-0.5900, 44.8000],
            [-0.5890, 44.8000],
            [-0.5890, 44.8010],
            [-0.5900, 44.8010],
            [-0.5900, 44.8000],
        ],
        800,
    ),
]

FEATURES_LANDES = [
    feature(
        "40192000CD0001",
        "1",
        [
            [-0.5000, 43.8900],
            [-0.4990, 43.8900],
            [-0.4990, 43.8910],
            [-0.5000, 43.8910],
            [-0.5000, 43.8900],
        ],
        1500,
        commune="40192",
    )
]


def construire_cadastre_test(
    repertoire: Path,
    configuration,
    features: list[dict],
) -> None:
    archive = repertoire / f"test-{configuration.code}.json.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as sortie:
        json.dump({"type": "FeatureCollection", "features": features}, sortie)
    construire_base_depuis_archive(
        archive,
        repertoire / configuration.nom_base,
        configuration=configuration,
        url_source=(
            "https://cadastre.data.gouv.fr/data/etalab-cadastre/2026-06-01/"
            f"geojson/departements/{configuration.code}/"
            f"cadastre-{configuration.code}-parcelles.json.gz"
        ),
    )


class CadastreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory()
        self.repertoire = Path(self.temporaire.name)
        archive = self.repertoire / "parcelles.json.gz"
        with gzip.open(archive, "wt", encoding="utf-8") as sortie:
            json.dump({"type": "FeatureCollection", "features": FEATURES}, sortie)
        base = self.repertoire / "cadastre_gironde.sqlite"
        nombre = construire_base_depuis_archive(
            archive,
            base,
            url_source=(
                "https://cadastre.data.gouv.fr/data/etalab-cadastre/2026-06-01/"
                "geojson/departements/33/cadastre-33-parcelles.json.gz"
            ),
        )
        self.assertEqual(nombre, 3)
        self.cadastre = CadastreGironde(self.repertoire)

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def test_recherche_par_reference_en_francais(self) -> None:
        parcelle = self.cadastre.rechercher_reference("33522", "ab", "0001")

        self.assertEqual(parcelle.identifiant, "33522000AB0001")
        self.assertIn("section AB", parcelle.reference)
        self.assertIn("parcelle n° 1", parcelle.reference)

    def test_recherche_par_clic(self) -> None:
        parcelle = self.cadastre.rechercher_point(44.8005, -0.5995)

        self.assertEqual(parcelle.numero, "1")

    def test_clic_sur_une_limite_est_refuse(self) -> None:
        with self.assertRaisesRegex(ErreurCadastre, "limite cadastrale"):
            self.cadastre.rechercher_point(44.8005, -0.5990)

    def test_fusionne_des_parcelles_adjacentes(self) -> None:
        premiere = self.cadastre.rechercher_reference("33522", "AB", "1")
        seconde = self.cadastre.rechercher_reference("33522", "AB", "2")

        resultat = fusionner_parcelles([premiere, seconde])

        self.assertEqual(resultat.surface_cadastrale, 2200)
        self.assertGreaterEqual(len(resultat.points), 4)
        self.assertEqual(resultat.geometrie_geojson["type"], "Polygon")

    def test_refuse_des_parcelles_separees(self) -> None:
        premiere = self.cadastre.rechercher_reference("33522", "AB", "1")
        troisieme = self.cadastre.rechercher_reference("33522", "AB", "3")

        with self.assertRaisesRegex(ErreurCadastre, "zone continue"):
            fusionner_parcelles([premiere, troisieme])

    def test_limite_le_nombre_de_parcelles(self) -> None:
        parcelle = self.cadastre.rechercher_reference("33522", "AB", "1")

        with self.assertRaisesRegex(ErreurCadastre, "limitée à 50 parcelles"):
            fusionner_parcelles([parcelle] * 51)

    def test_contour_cadastral_est_accepte_par_le_generateur(self) -> None:
        premiere = self.cadastre.rechercher_reference("33522", "AB", "1")
        seconde = self.cadastre.rechercher_reference("33522", "AB", "2")
        fusion = fusionner_parcelles([premiere, seconde])

        waypoints, lignes, _largeur, _hauteur = generate_waypoints_polygon(
            list(fusion.points),
            MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
        )

        self.assertGreater(len(waypoints), 0)
        self.assertGreater(lignes, 0)

    def test_collection_geojson_utilise_des_proprietes_francaises(self) -> None:
        parcelle = self.cadastre.rechercher_reference("33522", "AB", "1")

        collection = collection_geojson([parcelle])

        proprietes = collection["features"][0]["properties"]
        self.assertIn("référence", proprietes)
        self.assertIn("contenance_m²", proprietes)
        self.assertEqual(collection["features"][0]["id"], parcelle.cle)

    def test_metadonnees_de_la_base(self) -> None:
        informations = self.cadastre.informations()

        self.assertTrue(self.cadastre.est_installe)
        self.assertEqual(informations["departement"], "Gironde (33)")
        self.assertEqual(informations["code_departement"], "33")
        self.assertEqual(informations["version_controle_geometrique"], "1")
        self.assertEqual(informations["version_donnees"], "2026-06-01")

    def test_echec_indexation_conserve_ancienne_base(self) -> None:
        ancienne_base = self.cadastre.base.read_bytes()
        archive_vide = self.repertoire / "vide.json.gz"
        with gzip.open(archive_vide, "wt", encoding="utf-8") as sortie:
            json.dump({"type": "FeatureCollection", "features": []}, sortie)

        with self.assertRaisesRegex(ErreurCadastre, "Base cadastrale incomplète"):
            construire_base_depuis_archive(
                archive_vide,
                self.cadastre.base,
                nombre_minimum=1,
            )

        self.assertEqual(self.cadastre.base.read_bytes(), ancienne_base)
        self.assertTrue(self.cadastre.est_installe)

    def test_base_gironde_historique_reste_compatible(self) -> None:
        with closing(sqlite3.connect(self.cadastre.base)) as connexion:
            connexion.execute(
                "DELETE FROM metadonnees WHERE cle IN "
                "('code_departement', 'version_controle_geometrique')"
            )
            connexion.commit()

        self.assertTrue(self.cadastre.est_installe)

    def test_configuration_landes_utilise_des_fichiers_distincts(self) -> None:
        self.assertEqual(CADASTRE_LANDES.nom_base, "cadastre_landes.sqlite")
        self.assertIn("departements/40/", CADASTRE_LANDES.url)
        self.assertNotEqual(CADASTRE_GIRONDE.nom_base, CADASTRE_LANDES.nom_base)

    def test_construit_et_recherche_le_cadastre_des_landes(self) -> None:
        construire_cadastre_test(
            self.repertoire,
            CADASTRE_LANDES,
            FEATURES_LANDES,
        )
        landes = CadastreLandes(self.repertoire)

        parcelle = landes.rechercher_reference("40192", "AB", "1")
        par_clic = landes.rechercher_point(43.8905, -0.4995)

        self.assertTrue(landes.est_installe)
        self.assertEqual(parcelle.identifiant, "40192000CD0001")
        self.assertEqual(par_clic.code_departement, "40")
        self.assertIn("Landes (40)", parcelle.description)
        self.assertEqual(landes.informations()["departement"], "Landes (40)")

    def test_gironde_et_landes_coexistent_et_sont_routees_automatiquement(self) -> None:
        construire_cadastre_test(
            self.repertoire,
            CADASTRE_LANDES,
            FEATURES_LANDES,
        )
        landes = CadastreLandes(self.repertoire)
        regionaux = CadastresRegionaux((self.cadastre, landes))

        gironde = regionaux.rechercher_reference("33522", "AB", "1")
        lande = regionaux.rechercher_reference("40192", "AB", "1")

        self.assertEqual(regionaux.rechercher_point(44.8005, -0.5995), gironde)
        self.assertEqual(regionaux.rechercher_point(43.8905, -0.4995), lande)
        self.assertNotEqual(gironde.cle, lande.cle)
        self.assertTrue(self.cadastre.base.is_file())
        self.assertTrue(landes.base.is_file())

    def test_recherche_reference_indique_la_base_landes_manquante(self) -> None:
        regionaux = CadastresRegionaux((self.cadastre, CadastreLandes(self.repertoire)))

        with self.assertRaisesRegex(ErreurCadastre, "Installez.*Landes"):
            regionaux.rechercher_reference("40192", "AB", "1")

    def test_clic_ambigu_entre_deux_bases_est_refuse(self) -> None:
        landes_superposees = [
            feature(
                "40192000CD0001",
                "1",
                FEATURES[0]["geometry"]["coordinates"][0],
                1500,
                commune="40192",
            )
        ]
        construire_cadastre_test(
            self.repertoire,
            CADASTRE_LANDES,
            landes_superposees,
        )
        regionaux = CadastresRegionaux(
            (self.cadastre, CadastreLandes(self.repertoire))
        )

        with self.assertRaisesRegex(ErreurCadastre, "plusieurs départements"):
            regionaux.rechercher_point(44.8005, -0.5995)

    def test_archive_d_un_autre_departement_est_refusee(self) -> None:
        archive = self.repertoire / "mauvais-departement.json.gz"
        destination = self.repertoire / "mauvais.sqlite"
        with gzip.open(archive, "wt", encoding="utf-8") as sortie:
            json.dump(
                {"type": "FeatureCollection", "features": FEATURES_LANDES},
                sortie,
            )

        with self.assertRaisesRegex(ErreurCadastre, "incompatible"):
            construire_base_depuis_archive(
                archive,
                destination,
                configuration=CADASTRE_GIRONDE,
            )

        self.assertFalse(destination.exists())

    def test_metadonnee_d_un_mauvais_departement_desactive_la_base(self) -> None:
        with closing(sqlite3.connect(self.cadastre.base)) as connexion:
            connexion.execute(
                "UPDATE metadonnees SET valeur='40' WHERE cle='code_departement'"
            )
            connexion.commit()

        self.assertFalse(self.cadastre.est_installe)

    def test_installation_landes_utilise_sa_configuration(self) -> None:
        landes = CadastreLandes(self.repertoire)

        with (
            patch(
                "prog_vol.cadastre.telecharger_archive",
                return_value=(CADASTRE_LANDES.url, "sha-test"),
            ) as telecharger,
            patch(
                "prog_vol.cadastre.construire_base_depuis_archive",
                return_value=123,
            ) as construire,
        ):
            nombre = landes.installer()

        self.assertEqual(nombre, 123)
        self.assertEqual(telecharger.call_args.args[0].name, CADASTRE_LANDES.nom_archive)
        self.assertIs(
            telecharger.call_args.kwargs["configuration"],
            CADASTRE_LANDES,
        )
        self.assertEqual(construire.call_args.args[1], landes.base)
        self.assertIs(
            construire.call_args.kwargs["configuration"],
            CADASTRE_LANDES,
        )

    def test_fusion_accepte_des_parcelles_adjacentes_des_deux_departements(self) -> None:
        lande_adjacente = [
            feature(
                "40192000CD0001",
                "1",
                [
                    [-0.5980, 44.8000],
                    [-0.5970, 44.8000],
                    [-0.5970, 44.8010],
                    [-0.5980, 44.8010],
                    [-0.5980, 44.8000],
                ],
                1500,
                commune="40192",
            )
        ]
        construire_cadastre_test(
            self.repertoire,
            CADASTRE_LANDES,
            lande_adjacente,
        )
        gironde = self.cadastre.rechercher_reference("33522", "AB", "2")
        lande = CadastreLandes(self.repertoire).rechercher_reference(
            "40192", "AB", "1"
        )

        fusion = fusionner_parcelles([gironde, lande])

        self.assertEqual(fusion.geometrie_geojson["type"], "Polygon")
        self.assertEqual(fusion.surface_cadastrale, 2700)

    def test_reprise_d_archive_verifie_aussi_l_espace_disque(self) -> None:
        landes = CadastreLandes(self.repertoire)

        with patch(
            "prog_vol.cadastre.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(ErreurCadastre, "3 Go"):
                landes.installer()

    def test_archive_reutilisee_est_supprimee_si_l_indexation_echoue(self) -> None:
        landes = CadastreLandes(self.repertoire)
        archive = self.repertoire / CADASTRE_LANDES.nom_archive
        archive.write_bytes(b"archive invalide")

        with (
            patch("prog_vol.cadastre._archive_reutilisable", return_value=True),
            patch(
                "prog_vol.cadastre.construire_base_depuis_archive",
                side_effect=ErreurCadastre("archive tronquée"),
            ),
        ):
            with self.assertRaisesRegex(ErreurCadastre, "tronquée"):
                landes.installer()

        self.assertFalse(archive.exists())

    def test_indexation_refuse_des_coordonnees_tronquees(self) -> None:
        archive = self.repertoire / "coordonnees-tronquees.json.gz"
        destination = self.repertoire / "tronquee.sqlite"
        geometrie_aplatie = feature(
            "40192000CD0001",
            "1",
            [[-1, 43], [-1, 43], [-1, 43], [-1, 43]],
            1500,
            commune="40192",
        )
        with gzip.open(archive, "wt", encoding="utf-8") as sortie:
            json.dump(
                {"type": "FeatureCollection", "features": [geometrie_aplatie]},
                sortie,
            )

        with self.assertRaisesRegex(ErreurCadastre, "aplaties|précision"):
            construire_base_depuis_archive(
                archive,
                destination,
                configuration=CADASTRE_LANDES,
                nombre_minimum=1,
            )

        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
