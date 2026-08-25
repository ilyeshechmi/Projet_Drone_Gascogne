# Estimation de l'autonomie du drone

## 1. Objectif du calcul

La fonctionnalité d'autonomie de `prog_vol` répond à quatre questions avant la
génération et le transfert d'une mission :

1. quelle distance le drone doit-il parcourir ?
2. combien de temps la mission devrait-elle durer ?
3. une des batteries disponibles peut-elle effectuer la mission en continu tout
   en conservant une réserve à l'atterrissage ?
4. faut-il réduire ou découper la mission, puis changer de batterie au sol ?

Le résultat est une **estimation de préparation de mission**, pas une garantie.
Pendant le vol, les informations de DJI Fly, les alertes de batterie et le RTH
restent prioritaires.

## 2. Pourquoi utiliser principalement le temps

Trois approches ont été envisagées.

### 2.1 Distance seule

Une distance maximale ne suffit pas pour prévoir l'autonomie d'un multirotor.
Deux trajectoires de même longueur peuvent avoir des consommations différentes
à cause du vent, de la vitesse, de l'altitude, des virages et du vol stationnaire.

La distance publiée par DJI est obtenue dans des conditions de laboratoire, à
une vitesse choisie pour maximiser la portée. Elle ne correspond pas directement
à une mission photographique lente avec de nombreuses passes.

### 2.2 Nombre d'actions ou de photos

La prise d'une photo consomme peu par rapport à la propulsion. Il serait donc
incorrect d'utiliser une formule du type :

```text
énergie consommée = nombre de photos × énergie par photo
```

Les photos ont surtout un effet indirect :

- elles déterminent le nombre et l'espacement des waypoints ;
- elles peuvent imposer des ralentissements ou du vol stationnaire ;
- la caméra doit avoir terminé une prise avant la suivante ;
- des mouvements de nacelle ou des attentes peuvent allonger le vol.

### 2.3 Temps de vol

Le temps est la métrique principale retenue. La distance, la vitesse, l'altitude
et les actions servent à construire une durée de mission. Cette durée est ensuite
comparée à une autonomie de référence propre à chaque modèle de batterie.

## 3. Sources utilisées

### 3.1 Spécifications DJI Mini 5 Pro

Source officielle :

- [DJI Mini 5 Pro - Caractéristiques techniques](https://www.dji.com/fr/mini-5-pro/specs)
- [DJI Mini 5 Pro - FAQ](https://www.dji.com/fr/mini-5-pro/faq)
- [DJI Mini 5 Pro - Manuel utilisateur](https://dl.djicdn.com/downloads/DJI_Mini_5_Pro/UM/20251210/DJI_Mini_5_Pro_User_Manual_fr.pdf)

Les valeurs importantes publiées par DJI sont :

| Donnée | Batterie standard |
| --- | ---: |
| Temps de vol maximal | 36 min |
| Temps de vol typique | 21 min |
| Distance de vol maximale | 21 km |
| Résistance maximale au vent | 12 m/s |
| Capacité | 2 788 mAh |
| Tension nominale publiée | 7,0 V |
| Énergie publiée | 19,52 Wh |
| Température de fonctionnement | -10 à 40 °C |

Le temps maximal de 36 minutes est mesuré sans vent, à vitesse constante et
jusqu'à un atterrissage forcé provoqué par l'épuisement de la batterie. Cette
valeur n'est pas utilisée comme budget opérationnel sûr.

Le temps typique de 21 minutes est plus représentatif : DJI décrit un scénario
avec vent, montée, déplacement, caméra active, mouvements de nacelle, vol
stationnaire, apparition du rappel RTH puis atterrissage en sécurité. Il a donc
été retenu comme autonomie de référence initiale.

### 3.2 Profil de batterie utilisé par défaut

Le profil demandé pour l'application est :

| Donnée | Valeur configurée |
| --- | ---: |
| Modèle | `BWXNN3-2587-7.0` |
| Capacité | 2 788 mAh |
| Tension nominale | 7,70 V |
| Énergie | 21,4 Wh |
| Autonomie de référence | 21 min |
| Charge initiale | 100 % |

Les valeurs de tension et d'énergie diffèrent de la fiche DJI consultée. Elles
restent donc regroupées dans un profil modifiable plutôt que dispersées dans les
formules. L'estimation principale utilise l'autonomie de référence de 21 minutes,
pas une conversion directe des Wh en puissance moteur.

### 3.3 Méthodes générales d'estimation de batterie

Source complémentaire :

- [PX4 - Battery Estimation Tuning](https://docs.px4.io/main/en/config/battery.html)
- [Battery University - décharge et température](https://batteryuniversity.com/article/bu-502-discharging-at-high-and-low-temperatures)

PX4 rappelle qu'une estimation en vol fiable combine normalement tension,
compensation de la chute de tension sous charge et intégration du courant. Ces
données ne sont pas disponibles dans `prog_vol` avant le vol. La fonctionnalité
utilise donc un modèle prévisionnel plus simple et volontairement prudent.

## 4. Données d'entrée

### 4.1 Données de mission

Le calcul utilise :

- les coordonnées ordonnées des waypoints ;
- l'altitude relative ;
- la vitesse de mission ;
- le nombre de waypoints, donc de photos prévues ;
- l'intervalle minimal supposé entre deux photos ;
- le point Home, lorsqu'il est placé sur la carte.

### 4.2 Données de batterie

Chaque batterie de la mission possède :

- un nom temporaire, par exemple `Batterie 1` ;
- un modèle ou profil ;
- une capacité en mAh ;
- une tension nominale en V ;
- une énergie en Wh ;
- une autonomie de référence ;
- un niveau de charge entre 0 et 100 %.

Les batteries et leurs charges sont temporaires. Elles ne sont pas enregistrées
dans un fichier JSON et sont redemandées pour une nouvelle mission.

## 5. Calcul de l'énergie d'un profil personnalisé

Lorsque la capacité et la tension sont connues, l'énergie théorique est :

```text
E_Wh = capacité_mAh × tension_V / 1000
```

Pour le profil par défaut fourni :

```text
E = 2788 × 7,70 / 1000
E ≈ 21,47 Wh
```

La valeur configurée de 21,4 Wh est cohérente avec ce calcul après arrondi.

Si l'utilisateur renseigne une autonomie réellement mesurée, celle-ci est
utilisée directement. Sinon, une première approximation est calculée par rapport
au profil par défaut :

```text
T_ref_personnalisée = T_ref_défaut × E_personnalisée / E_défaut
```

Avec :

```text
T_ref_défaut = 21 min
E_défaut = 21,4 Wh
```

Cette extrapolation est marquée comme moins fiable. Elle suppose implicitement
que la batterie est compatible, que son poids reste comparable et que le drone
a une consommation moyenne similaire.

## 6. Calcul géographique des distances

La distance entre deux coordonnées est calculée avec la formule de Haversine.

Pour deux points de latitude/longitude `(φ1, λ1)` et `(φ2, λ2)` en radians :

```text
Δφ = φ2 - φ1
Δλ = λ2 - λ1

a = sin²(Δφ / 2)
    + cos(φ1) × cos(φ2) × sin²(Δλ / 2)

c = 2 × atan2(√a, √(1 - a))

d = R_terre × c
```

Le rayon utilisé est :

```text
R_terre = 6 371 000 m
```

Cette précision est suffisante pour les distances locales des missions.

## 7. Distance de la trajectoire

Pour `n` waypoints ordonnés :

```text
D_zone = Σ distance(WP_i, WP_i+1)
```

pour `i` allant de `1` à `n - 1`.

Cette somme inclut :

- les segments photographiques le long des passes ;
- les liaisons entre deux passes ;
- les transitions produites par l'ordre actuel des waypoints.

Elle n'inclut pas le trajet vers la zone ni le retour au Home.

## 8. Prise en compte du Home

Le Home est facultatif et représente actuellement le même point pour le
décollage et l'atterrissage.

Lorsqu'il est renseigné :

```text
D_transit = distance(Home, WP_1)
            + distance(WP_n, Home)
```

La distance horizontale totale devient :

```text
D_totale = D_zone + D_transit
```

Le Home sert uniquement à l'estimation. Dans cette version, il ne change ni
l'orientation des passes, ni leur point de départ, ni l'ordre des waypoints.

Une évolution future pourra choisir le sens de parcours qui minimise :

```text
distance(Home, premier waypoint)
+ distance de couverture
+ distance(dernier waypoint, Home)
```

## 9. Calcul de la durée

### 9.1 Durée de la trajectoire dans la zone

```text
T_zone = D_zone / vitesse_mission
```

### 9.2 Durée du transit horizontal

Lorsque le Home existe :

```text
T_transit = D_transit / vitesse_mission
```

La vitesse utilisée est celle écrite dans la mission et dans les paramètres de
transition WPML.

### 9.3 Montée et descente

Le calcul utilise actuellement une hypothèse prudente de 3 m/s pour la montée et
la descente :

```text
T_vertical = altitude / 3 + altitude / 3
```

soit :

```text
T_vertical = 2 × altitude / 3
```

### 9.4 Durée complète

Avec Home :

```text
T_mission = T_zone + T_transit + T_vertical
```

Sans Home :

```text
T_mission_minimale = T_zone
```

Cette seconde valeur est un minimum incomplet. Le programme ne peut pas afficher
un statut vert lorsque le Home est absent.

## 10. Photos et cadence

Le générateur place actuellement une action `takePhoto` à chaque waypoint :

```text
nombre_photos_prévues = nombre_waypoints
```

Le drone ne reçoit pas de commande d'attente systématique à chaque photo. Le
temps photographique n'est donc pas additionné artificiellement à la durée de
vol.

Le programme contrôle cependant chaque segment :

```text
intervalle_i = distance(WP_i, WP_i+1) / vitesse_mission
```

Un segment est signalé comme trop court si :

```text
intervalle_i < intervalle_photo_supposé
```

Les valeurs proposées dans l'interface sont :

| Cadence supposée | Intervalle minimal |
| --- | ---: |
| 12 MP | 2 s |
| 50 MP | 5 s |

Ce choix sert uniquement au contrôle de cadence. Il ne configure pas la
résolution de la caméra dans le KMZ. La caméra doit être réglée correctement dans
DJI Fly.

## 11. Autonomie disponible par batterie

Soit :

- `T_ref` : autonomie de référence à 100 % ;
- `C` : niveau de charge saisi, en pourcentage ;
- `R` : charge à conserver à l'atterrissage, en points de pourcentage.

### 11.1 Autonomie brute

```text
T_brute = T_ref × C / 100
```

### 11.2 Autonomie sûre

La réserve est un seuil de charge final. Avec une réserve de 20 %, le programme
cherche à atterrir avec environ 20 % de batterie restante.

```text
charge_utilisable = max(C - R, 0)

T_sûre = T_ref × charge_utilisable / 100
```

Il ne faut pas utiliser :

```text
T_brute × (1 - R / 100)
```

Cette autre formule conserverait seulement 20 % de la charge disponible. Pour
une batterie à 50 %, elle autoriserait 40 points de charge au lieu de 30 et ne
garantirait donc pas un atterrissage à 20 %.

### 11.3 Exemples avec `T_ref = 21 min` et `R = 20 %`

| Charge au départ | Charge utilisable | Autonomie sûre |
| ---: | ---: | ---: |
| 100 % | 80 % | 16 min 48 s |
| 80 % | 60 % | 12 min 36 s |
| 50 % | 30 % | 6 min 18 s |
| 30 % | 10 % | 2 min 06 s |
| 20 % | 0 % | 0 min |

## 12. Logique des alertes

### 12.1 Estimation complète avec Home

Pour chaque batterie, le programme calcule `T_brute` et `T_sûre`.

#### Niveau vert : mission réalisable

```text
il existe une batterie telle que T_sûre >= T_mission
```

La batterie ayant la plus grande autonomie sûre est recommandée et la marge est :

```text
marge = T_sûre - T_mission
```

#### Proposition de découpage : réserve non respectée

```text
aucune batterie n'a T_sûre >= T_mission
mais une batterie a T_brute >= T_mission
```

La mission pourrait théoriquement tenir sur cette batterie, mais elle entamerait
la charge réservée à l'atterrissage. Le programme recherche donc aussi un
découpage utilisant les batteries déclarées et respectant la réserve.

#### Proposition de découpage : plusieurs batteries nécessaires

```text
aucune batterie n'a T_brute >= T_mission
mais Σ T_sûre >= T_mission
```

La somme reste un premier indicateur, mais le programme ne la considère plus
comme une validation. Il recherche des segments contigus et vérifie séparément :

```text
T_partie = T_montée + T_transit_aller + T_zone_partie
          + T_retour + T_descente
```

Une batterie physique est utilisée au plus une fois. Les coupures entre passes
sont prioritaires. Une coupure au milieu d'une passe est autorisée uniquement si
aucun plan par passes entières n'est réalisable. L'utilisateur valide la
proposition avant la création des fichiers KMZ séparés. La recherche compare au
maximum 12 batteries et favorise successivement le plus petit nombre de parties,
la meilleure marge minimale puis le transit total le plus court.

#### Niveau rouge : découpage impossible

```text
Σ T_sûre < T_mission
```

La charge déclarée ne permet pas de couvrir la mission avec la réserve demandée,
ou les surcoûts des sorties supplémentaires rendent tout découpage impossible.

### 12.2 Estimation sans Home

Sans Home, seuls `D_zone` et `T_zone` sont connus.

Deux situations sont distinguées :

```text
si T_zone > meilleure autonomie sûre : niveau critique
sinon : estimation partielle
```

Le niveau critique est justifié parce que la partie connue dépasse déjà la
capacité sûre, avant même d'ajouter la montée, le transit et le retour.

L'estimation partielle n'est jamais présentée comme un accord de vol.

## 13. Exemples de décision

### Exemple A : une batterie pleine, mission de 10 minutes

```text
T_ref = 21 min
C = 100 %
R = 20 %

T_sûre = 21 × (100 - 20) / 100
T_sûre = 16,8 min

marge = 16,8 - 10
marge = 6,8 min
```

Résultat : mission réalisable avec une batterie.

### Exemple B : une batterie à 50 %, mission de 10 minutes

```text
T_brute = 21 × 50 / 100 = 10,5 min
T_sûre = 21 × (50 - 20) / 100 = 6,3 min
```

Résultat : la durée théorique est suffisante, mais la réserve ne sera pas
respectée. Une alerte orange est affichée.

### Exemple C : deux batteries à 50 %, mission de 12 minutes

Pour chaque batterie :

```text
T_brute = 10,5 min
T_sûre = 6,3 min
```

Aucune batterie ne couvre les 12 minutes seule, mais :

```text
Σ T_sûre = 6,3 + 6,3 = 12,6 min
```

Résultat : au moins deux batteries semblent nécessaires. Le programme avertit
qu'un atterrissage et un découpage sont obligatoires et que les transits
supplémentaires ne sont pas encore inclus.

## 14. Traduction dans le code

### `autonomy.py`

Ce module contient :

- `BatteryProfile` : caractéristiques d'un modèle de batterie ;
- `BatteryState` : profil et charge d'une batterie de la mission ;
- `FlightEstimate` : distances, durées, photos et complétude du calcul ;
- `BatteryAssessment` : niveau d'alerte, recommandation et marge ;
- `geographic_distance_m()` : formule de Haversine ;
- `estimate_flight()` : calcul de distance et de durée ;
- `assess_batteries()` : logique des alertes.

### `generator.py`

Le générateur :

- produit les waypoints sans utiliser le Home pour les réordonner ;
- appelle `estimate_flight()` ;
- renseigne les champs WPML `distance` et `duration` pour la wayline ;
- ajoute `flight_estimate` au `GenerationResult`.

Les champs WPML correspondent à la trajectoire de la wayline. Le transit Home et
la montée/descente servent au budget d'autonomie affiché par l'application, mais
ne sont pas ajoutés à la distance interne de la wayline.

### `map_widget.py`

La carte permet de placer ou supprimer un marqueur Home distinct du polygone.
Ce marqueur est transmis à Python sans modifier le dessin de la couverture.

### `gui.py`

L'interface :

- crée une batterie par défaut à 100 % ;
- permet d'ajouter ou supprimer des batteries ;
- accepte un profil personnalisé ;
- demande une réserve de charge finale ;
- présente les distances, la durée, les photos, la batterie recommandée et la
  marge ;
- conserve les avertissements dans l'onglet de transfert ;
- demande une confirmation renforcée avant le transfert d'une mission critique.

## 15. Limites connues

Le modèle ne connaît pas encore :

- la vitesse et la direction réelles du vent ;
- la température de la batterie ;
- le vieillissement et la résistance interne de chaque batterie ;
- les cycles de charge ;
- la consommation instantanée des moteurs ;
- les attentes réelles imposées par DJI Fly ;
- les détours dus à l'évitement d'obstacles ;
- le relief et les variations d'altitude du terrain ;
- les surcoûts exacts d'une mission découpée ;
- la consommation d'une batterie sous charge à basse température.

Une batterie personnalisée est estimée par les Wh si aucune autonomie mesurée
n'est fournie. Cette proportionnalité ne tient pas compte du poids supplémentaire
ni d'une éventuelle incompatibilité avec le drone.

## 16. Améliorations futures

### 16.1 Calibration avec des vols réels

Après un vol, une autonomie équivalente à 100 % peut être estimée par :

```text
T_équivalente_100 = durée_réelle × 100
                    / (charge_départ - charge_arrivée)
```

Exemple :

```text
durée = 12 min
charge départ = 95 %
charge arrivée = 35 %
consommation = 60 points

T_équivalente_100 = 12 × 100 / 60 = 20 min
```

Plusieurs vols comparables permettraient d'utiliser une valeur prudente, par
exemple un percentile bas, plutôt qu'une simple moyenne.

### 16.2 Adaptation de la trajectoire au Home

Une version future pourra :

- inverser l'ordre des waypoints ;
- commencer du côté le plus proche du Home ;
- terminer du côté facilitant le retour ;
- comparer plusieurs orientations de balayage ;
- minimiser la distance totale et le nombre de virages.

Cette optimisation est volontairement exclue de la version actuelle.

### 16.3 Découpage automatique

Le programme conserve désormais les frontières des passes et découpe la liste
des waypoints en sous-missions contiguës. Chaque sous-mission inclut son propre
transit, sa montée, son retour et sa descente, reçoit une batterie physique, puis
est générée et validée séparément. L'aperçu utilise une couleur par partie et les
fichiers sont transférés individuellement vers DJI Fly.

## 17. Règle opérationnelle finale

L'estimation doit être interprétée comme une aide à la décision :

```text
estimation prog_vol
+ état réel de la batterie
+ météo sur le site
+ alertes DJI Fly
+ jugement du télépilote
= décision de décollage et poursuite du vol
```

Une alerte verte signifie seulement que les données saisies et les hypothèses du
modèle laissent une marge suffisante. Elle ne remplace jamais la surveillance de
la batterie et de la possibilité de retour pendant le vol.
