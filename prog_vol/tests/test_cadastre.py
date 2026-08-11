"""Tests de la base cadastrale locale sans téléchargement réseau."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from prog_vol.cadastre import (
    CadastreGironde,
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
) -> dict:
    return {
        "type": "Feature",
        "id": identifiant,
        "properties": {
            "id": identifiant,
            "commune": "33522",
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

    def test_metadonnees_de_la_base(self) -> None:
        informations = self.cadastre.informations()

        self.assertTrue(self.cadastre.est_installe)
        self.assertEqual(informations["departement"], "Gironde (33)")
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


if __name__ == "__main__":
    unittest.main()
