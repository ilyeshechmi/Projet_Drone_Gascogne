"""Permet de lancer l'application avec python -m drone_importer."""

from .main import run

# SystemExit renvoie correctement le code de succès ou d'erreur au terminal.
raise SystemExit(run())
