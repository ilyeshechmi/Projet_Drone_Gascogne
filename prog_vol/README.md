# Projet Drone Gascogne - Génération et transfert de missions DJI

Cette application réunit dans une seule interface graphique les deux étapes qui
étaient auparavant séparées :

1. dessiner une zone, calculer une trajectoire et créer un fichier DJI KMZ ;
2. détecter une radiocommande, choisir une mission DJI Fly existante, sauvegarder
   son fichier puis le remplacer de manière vérifiée.

La version actuelle utilise OpenStreetMap et permet aussi d'installer localement
les parcelles cadastrales officielles de Gironde afin de sélectionner une zone
par clic ou par référence cadastrale.

## Avertissement de sécurité

Une mission automatique doit toujours être contrôlée dans DJI Fly avant le vol :
altitudes, obstacles, trajectoire, orientation de la nacelle, zone réglementée,
autonomie et comportement en cas de perte de liaison.

Le validateur de cette application contrôle la structure technique principale du
KMZ. Il ne remplace ni la validation de DJI Fly, ni l'analyse des contraintes de
terrain et de la réglementation aérienne.

## Fonctionnalités

- fenêtre PyQt5 unique avec deux onglets ;
- recherche d'un lieu avec Nominatim ;
- carte Leaflet et fond OpenStreetMap ;
- dessin d'un polygone libre comportant au moins trois sommets ;
- téléchargement et indexation locale des parcelles cadastrales de Gironde ;
- recherche par code INSEE, préfixe, section et numéro de parcelle ;
- sélection par clic et fusion des parcelles adjacentes ;
- calcul d'une trajectoire en boustrophédon ;
- aperçu des waypoints et de la trajectoire ;
- génération d'une archive `wpmz/template.kml` + `wpmz/waylines.wpml` ;
- validation immédiate du KMZ produit ;
- transmission automatique du KMZ généré vers l'onglet de transfert ;
- sélection manuelle d'un autre KMZ ;
- détection des volumes montés, de GIO/MTP et de libmtp ;
- affichage des missions présentes dans DJI Fly ;
- choix explicite de la mission cible ;
- sauvegarde locale, remplacement, vérification SHA-256 et restauration ;
- CLI conservée pour les diagnostics et l'automatisation.

## Prérequis

- Python 3.10 ou plus récent ;
- PyQt5 ;
- PyQtWebEngine ;
- geopy ;
- ijson et Shapely pour l'indexation et les calculs cadastraux ;
- une connexion Internet pour le géocodage, Leaflet et les tuiles OpenStreetMap ;
- libmtp sur macOS lorsque la radiocommande n'est pas montée comme un volume.

L'environnement virtuel compatible se trouve dans `../env_drone/`, à côté du
dossier `Projet_Drone_Gascogne/`.

L'installation cadastrale télécharge environ 236 Mo. Il faut prévoir au moins
3 Go libres pendant la construction de la base locale. La base est conservée
hors du dépôt, dans le dossier de données de l'utilisateur.

### Installation avec l'environnement existant

Depuis la racine `Projet_Drone_Gascogne/` :

```bash
source ../env_drone/bin/activate
python -m pip install -r prog_vol/requirements.txt
```

### Création d'un nouvel environnement

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r prog_vol/requirements.txt
```

### Installation de libmtp sur macOS

```bash
brew install libmtp
```

Fermer OpenMTP et les autres applications utilisant la radiocommande avant de
lancer la détection. Un périphérique MTP ne peut généralement être ouvert que par
une application à la fois.

## Lancement graphique

La commande doit être exécutée depuis le dossier parent du paquet, donc depuis la
racine `Projet_Drone_Gascogne/` :

```bash
source ../env_drone/bin/activate
python -m prog_vol
```

Le point d'entrée `prog_vol/__main__.py` lance `prog_vol.gui.run_gui()`.

## Préparation de DJI Fly

DJI Fly associe chaque mission à un identifiant interne et à un chemin de la
forme suivante :

```text
Android/data/dji.go.v5/files/waypoint/IDENTIFIANT/IDENTIFIANT.kmz
```

L'application ne crée pas cette entrée dans la base de données DJI Fly. Avant le
premier transfert :

1. créer dans DJI Fly une mission brouillon avec deux ou trois waypoints ;
2. enregistrer cette mission ;
3. fermer la vue d'édition de la mission ;
4. brancher la radiocommande en USB ;
5. choisir le mode Android de transfert de fichiers si la radio le demande ;
6. lancer la détection depuis l'application Projet Drone Gascogne.

La mission brouillon sert de cible. Son KMZ est sauvegardé puis remplacé, mais son
nom et son emplacement restent ceux attendus par DJI Fly.

## Onglet 1 - Génération de mission

### 1. Localiser la zone

Saisir un lieu, par exemple `ENSEIRB-MATMECA, Bordeaux`, puis cliquer sur
`Localiser`. La recherche est exécutée dans un thread afin de ne pas bloquer la
fenêtre.

Si la recherche échoue, la carte reste utilisable et peut être déplacée
manuellement.

### 2. Utiliser les parcelles cadastrales de Gironde

Le dessin libre reste disponible. Pour utiliser le cadastre :

1. cliquer sur `Installer les données cadastrales` lors de la première utilisation ;
2. attendre la fin du téléchargement et de l'indexation locale ;
3. choisir `Sélection cadastrale` ;
4. cliquer au centre d'une parcelle sur la carte, ou renseigner son code INSEE,
   son préfixe éventuel, sa section et son numéro ;
5. sélectionner d'autres parcelles adjacentes si nécessaire.

Un second clic sur une parcelle déjà sélectionnée la retire. Les parcelles qui ne
se touchent pas sont refusées dans cette première version afin de ne pas créer de
transition de vol hors de la zone demandée. La contenance affichée vient du
cadastre; elle est donnée en mètres carrés.

La base SQLite est enregistrée sous macOS dans :

```text
~/Library/Application Support/Projet Drone Gascogne/cadastre/
```

Le fichier compressé est supprimé après l'indexation. Les données locales peuvent
être remplacées avec le bouton `Mettre à jour les données cadastrales`.

### 3. Régler les paramètres

Les paramètres disponibles sont :

| Paramètre | Utilisation |
| --- | --- |
| Altitude | hauteur relative écrite dans chaque waypoint |
| Vitesse | vitesse des waypoints et vitesse de transition |
| Nacelle | angle de tangage, de `-90°` vers le sol à `0°` horizontal |
| Recouvrement frontal | espacement entre les prises de vue d'une passe |
| Recouvrement latéral | espacement entre deux passes |
| Largeur du capteur | calcul de l'empreinte au sol |
| Hauteur du capteur | calcul de l'empreinte au sol |
| Longueur focale | calcul de l'empreinte au sol |

Les recouvrements affichés en pourcentage sont convertis en nombres entre `0` et
`1` avant le calcul.

### 4. Dessiner librement la zone

Cliquer sur la carte pour ajouter les sommets dans l'ordre du contour. Le tracé
ne doit pas se croiser lui-même.

Après au moins trois sommets :

1. cliquer sur `Fermer le polygone` ;
2. vérifier visuellement la zone fermée ;
3. utiliser `Réinitialiser` en cas d'erreur.

### 5. Générer le KMZ

Cliquer sur `Générer le fichier KMZ`, puis choisir son emplacement. La génération
est réalisée en arrière-plan.

Le programme :

1. valide les paramètres et le contour ;
2. calcule les lignes de balayage ;
3. produit les waypoints en alternant leur direction ;
4. limite la mission à 20 000 waypoints pour protéger la mémoire ;
5. écrit le KMZ ;
6. relit et valide l'archive ;
7. affiche les waypoints sur la carte ;
8. sélectionne le fichier dans l'onglet de transfert.

Aucun message de réussite n'est affiché avant la création et la validation
effectives du fichier.

## Onglet 2 - Radiocommande et transfert

### 1. Détecter la radiocommande

Cliquer sur `Détecter / Actualiser`. La détection teste dans cet ordre :

1. les volumes présents sous `/Volumes` ;
2. les URI MTP exposés par `gio` ;
3. l'accès natif avec libmtp.

Un stockage n'est considéré comme compatible que si le dossier waypoint de DJI
Fly est accessible.

### 2. Choisir une mission cible

La liste présente les KMZ trouvés avec :

- leur identifiant ou leur nom ;
- leur date de modification lorsqu'elle est disponible ;
- leur taille ;
- leur chemin relatif sous le dossier waypoint.

Une seule mission peut être sélectionnée. La mission la plus récente est placée
en tête, mais le choix reste toujours explicite.

Actualiser la liste après toute création ou modification effectuée dans DJI Fly.

### 3. Choisir le KMZ source

Le fichier produit dans le premier onglet est sélectionné automatiquement. Le
bouton `Changer le fichier KMZ` permet d'utiliser un autre fichier local.

Avant d'activer le transfert, le programme vérifie :

- l'extension `.kmz` ;
- la validité de l'archive ZIP ;
- la présence de `wpmz/template.kml` ;
- la présence de `wpmz/waylines.wpml` ;
- les racines KML et les configurations WPML essentielles ;
- la présence des coordonnées, index et altitudes des waypoints ;
- les limites de taille et les chemins dangereux dans l'archive.

Le nombre de waypoints, la taille et un extrait du SHA-256 sont affichés.

### 4. Confirmer et transférer

Le bouton `Transférer vers DJI Fly` devient actif uniquement lorsqu'une source
valide et une cible sont sélectionnées.

La confirmation récapitule la source, le nombre de waypoints et la cible. Le
programme vérifie ensuite que :

- la radiocommande est toujours la même ;
- la mission cible existe toujours ;
- sa taille et sa date n'ont pas changé depuis la détection ;
- le KMZ transféré correspond exactement aux octets validés et confirmés.

Si un de ces contrôles échoue, aucun remplacement n'est effectué et il faut
actualiser la liste.

## Sécurité du remplacement

Le transfert normal suit les étapes suivantes :

1. téléchargement de l'ancienne mission dans `prog_vol/backups/` ;
2. comparaison du SHA-256 local avec celui de la cible ;
3. écriture d'un manifeste JSON à côté de la sauvegarde ;
4. matérialisation des octets validés dans un fichier temporaire ;
5. envoi sous un nom temporaire sur la radiocommande ;
6. vérification du temporaire ;
7. conservation temporaire de l'ancienne cible distante ;
8. renommage de la nouvelle cible ;
9. comparaison de la taille et du SHA-256 final ;
10. suppression de la copie distante temporaire après réussite.

En cas d'échec après le début du remplacement, le backend tente de restaurer
l'ancienne mission et vérifie cette restauration. Ne pas débrancher la
radiocommande pendant l'opération.

La fenêtre ne peut pas être fermée tant qu'une opération est active.

## Interface en ligne de commande

La CLI historique reste disponible :

```bash
python -m prog_vol.main mission_waypoints.kmz --dry-run
python -m prog_vol.main mission_waypoints.kmz
```

Options principales :

```text
--dry-run                 simule sans écrire sur la radiocommande
--target CHEMIN           choisit explicitement la cible
--device-root CHEMIN      utilise un faux stockage ou un volume monté
--mtp-uri URI             utilise un URI GIO/MTP précis
--waypoint-dir CHEMIN     remplace le dossier waypoint détecté
--yes                     confirme sans question interactive
--verbose                 active un journal plus détaillé
```

Afficher l'aide complète :

```bash
python -m prog_vol.main --help
```

## Architecture du code

```text
prog_vol/
├── __init__.py       API Python publique
├── __main__.py       point d'entrée de l'interface graphique
├── gui.py            fenêtre, onglets et workers QThread
├── map_widget.py     Leaflet, QWebEngineView et QWebChannel
├── cadastre.py       téléchargement, SQLite/RTree et géométries cadastrales
├── generator.py      paramètres, trajectoire et export KMZ
├── missions.py       inspection et validation des archives
├── mtp.py            volumes montés, GIO/MTP et libmtp natif
├── transfer.py       plan, sauvegarde, remplacement et restauration
├── requirements.txt dépendances Python de l'application
├── tests/            tests unitaires et d'intégration locale
├── backups/          anciennes missions et manifestes JSON
├── logs/             journal tournant
└── tmp/              fichiers temporaires contrôlés
```

L'ancien `git/prog_vol.py` reste un prototype historique. Le code actif de la
nouvelle application se trouve dans le paquet `prog_vol/`.

## Modifier ou ajouter un générateur

La logique de génération ne dépend pas de PyQt. L'interface appelle le point
d'entrée suivant :

```python
from prog_vol.generator import MissionParameters, generate_mission

result = generate_mission(polygon, parameters, output_path)
```

Entrées :

- `polygon` : liste de couples `(latitude, longitude)` ;
- `parameters` : instance immuable de `MissionParameters` ;
- `output_path` : chemin de sortie du KMZ.

Sortie :

- `GenerationResult.output_path` ;
- `GenerationResult.waypoints` ;
- `GenerationResult.line_count` ;
- `GenerationResult.fov_width` ;
- `GenerationResult.fov_height` ;
- `GenerationResult.waypoint_count`.

Pour remplacer l'algorithme sans toucher au transfert, conserver ce contrat ou
ajouter une fonction qui retourne le même `GenerationResult`. Toute nouvelle
sortie doit être contrôlée par `inspect_mission()` avant d'être proposée au
transfert.

## Tests

Depuis `Projet_Drone_Gascogne/` avec l'environnement virtuel voisin :

```bash
PYTHONDONTWRITEBYTECODE=1 ../env_drone/bin/python -m unittest discover -s prog_vol/tests -v
```

Les tests couvrent notamment :

- la production d'un KMZ accepté par le validateur ;
- les paramètres et polygones invalides ;
- le rejet d'un contour auto-intersecté ;
- les protections contre les archives dangereuses ;
- le `dry-run` ;
- la sauvegarde et la vérification du transfert ;
- l'immutabilité des octets validés ;
- la restauration après échec ;
- la détection de faux stockages ;
- le tri et les chemins relatifs utilisés par l'interface ;
- les chemins virtuels libmtp confinés.
- l'indexation cadastrale sans réseau ;
- la recherche par référence et par clic ;
- la fusion des parcelles adjacentes et le refus des parcelles séparées.

Les tests de transfert utilisent un faux stockage local et ne modifient pas une
vraie radiocommande.

### Test minimal de construction de la fenêtre

Sur une machine sans écran graphique :

```bash
QT_QPA_PLATFORM=offscreen \
QTWEBENGINE_DISABLE_SANDBOX=1 \
../env_drone/bin/python -c \
'from PyQt5.QtWidgets import QApplication; from prog_vol.gui import MainWindow; app=QApplication([]); window=MainWindow(); print(window.tabs.count()); window.close()'
```

Le résultat attendu est `2`.

## Journaux et sauvegardes

Le journal tournant est écrit dans :

```text
prog_vol/logs/mission_transfer.log
```

Les anciennes missions sont écrites dans :

```text
prog_vol/backups/DATE_IDENTIFIANT.kmz
prog_vol/backups/DATE_IDENTIFIANT.kmz.json
```

Le manifeste JSON contient la source, la cible, les chemins de sauvegarde et les
checksums. Conserver ces fichiers au moins jusqu'à la vérification de la mission
dans DJI Fly.

## Dépannage

### La carte reste vide

Vérifier la connexion Internet et l'accès à `unpkg.com` et
`tile.openstreetmap.org`. Leaflet et le fond de carte sont actuellement chargés
depuis Internet.

### Le lieu n'est pas trouvé

Utiliser une requête plus précise avec ville et pays, ou déplacer manuellement la
carte. Nominatim peut aussi limiter temporairement les requêtes.

### Les données cadastrales ne sont pas installées

Cliquer sur `Installer les données cadastrales`, vérifier la connexion Internet
et conserver au moins 3 Go libres pendant l'opération. L'installation utilise le
fichier officiel des parcelles de Gironde publié sur cadastre.data.gouv.fr.

### Une parcelle n'est pas trouvée au clic

Zoomer et cliquer davantage au centre de la parcelle. Un clic posé exactement sur
une limite peut correspondre à plusieurs parcelles. Pour la recherche textuelle,
vérifier le code INSEE, la section, le numéro et, si nécessaire, le préfixe.

### Aucune radiocommande n'est détectée

- vérifier le câble USB et le mode transfert de fichiers ;
- fermer OpenMTP ou toute application utilisant déjà le périphérique ;
- installer libmtp ;
- vérifier qu'une mission brouillon existe dans DJI Fly ;
- relancer la détection après avoir enregistré la mission.

### Plusieurs stockages DJI sont détectés

La détection automatique refuse de choisir à la place de l'utilisateur. Débrancher
les périphériques inutiles ou utiliser la CLI avec `--device-root` ou `--mtp-uri`.

### La cible a changé depuis la détection

Le transfert est volontairement annulé. Cliquer sur `Détecter / Actualiser`,
resélectionner la mission et confirmer à nouveau.

### Le transfert échoue

Ne pas supprimer les fichiers de `backups/`. Consulter le journal, vérifier que
la cible a été restaurée, puis ouvrir DJI Fly avant toute nouvelle tentative.

## Limites actuelles

- données cadastrales limitées au département de la Gironde ;
- seules les parcelles adjacentes formant un polygone continu sans trou sont acceptées ;
- carte et géocodage dépendants d'Internet ;
- génération limitée à un balayage horizontal ;
- modèle de drone WPML actuellement fixé à la valeur utilisée par le prototype ;
- les zones concaves doivent être vérifiées avec une attention particulière car
  une liaison entre deux passes peut sortir brièvement du contour ;
- aucun calcul d'obstacles, de relief, d'autonomie ou de réglementation ;
- la trajectoire doit être inspectée dans DJI Fly avant utilisation.
