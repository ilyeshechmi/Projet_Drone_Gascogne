DJI PHOTO IMPORTER - GUIDE UTILISATEUR
======================================

1. A QUOI SERT L'APPLICATION ?
------------------------------

DJI Photo Importer permet de brancher directement un drone DJI en USB sur un
Mac, de trouver ses photos originales et d'importer uniquement les nouvelles
photos sous forme de missions.

L'application :

- detecte le drone DJI et son stockage USB ;
- recherche toutes les photos JPG, JPEG et DNG ;
- lit DateTimeOriginal dans les metadonnees EXIF ;
- utilise la date de modification si la date EXIF est absente ;
- reconnait les photos deja importees ;
- regroupe les photos chronologiquement en missions ;
- copie les missions selectionnees dans des dossiers separes ;
- verifie chaque copie avant de l'ajouter a l'historique.

Les originaux presents sur le drone sont uniquement lus. Ils ne sont jamais
supprimes, deplaces, renommes, modifies ou recompresses.


2. PREPARATION DU MAC
---------------------

Il faut :

- macOS ;
- Python 3 ;
- PyQt5 version 5.15 ou plus recente ;
- un cable USB permettant le transfert de donnees ;
- un drone DJI dont le stockage est monte par macOS.

PyQt5 est deja installe dans l'environnement env_drone de ce projet. Aucune
installation supplementaire n'a ete effectuee pendant ce developpement.

Sur un autre Mac, si PyQt5 manque, ouvrir Terminal dans le dossier du projet et
executer une seule fois :

    python3 -m pip install -r requirements-drone-importer.txt



3. LANCEMENT SIMPLE PAR DOUBLE-CLIC
----------------------------------

1. Allumer le drone.
2. Brancher le drone au Mac avec le cable USB de donnees.
3. Attendre que son volume apparaisse dans le Finder.
4. Double-cliquer sur :

    DJI Photo Importer.command

Le lanceur recherche automatiquement env_drone dans le dossier de l'application
et dans ses deux dossiers parents, puis utilise python3 installe sur le Mac si
aucun de ces environnements n'existe.

Les composants PyQt5, Qt5 et WebEngine de env_drone sont alignes sur des
bibliotheques Qt 5.15.19 compatibles entre elles.

macOS peut demander l'autorisation d'ouvrir le fichier, d'acceder aux volumes
amovibles ou d'ecrire sur le Bureau. Il faut accepter ces autorisations.

Le lancement manuel reste possible :

    python3 drone_photo_importer.py


4. UTILISATION DE L'INTERFACE
-----------------------------

Au demarrage, l'application verifie la connexion du drone.

Cliquer sur "Analyser le drone". L'analyse est executee en arriere-plan afin de
ne pas bloquer la fenetre.

Le resume indique :

- le nombre total de photos trouvees ;
- le nombre de photos deja importees ;
- le nombre de nouvelles photos ;
- le nombre de missions detectees.

Toutes les nouvelles missions sont selectionnees par defaut. Les boutons "Tout
selectionner" et "Tout deselectionner" permettent de modifier rapidement la
selection.

Cliquer ensuite sur "Importer les missions selectionnees". La barre de
progression affiche la mission, le fichier en cours et l'avancement global.


5. PARAMETRES AVANCES
---------------------

La section "Parametres avances" contient :

- le seuil de separation des missions, 5 minutes par defaut ;
- l'import des fichiers JPG/JPEG, active par defaut ;
- l'import des fichiers DNG, active par defaut ;
- l'exclusion des photos deja importees, activee par defaut.

Si les parametres sont modifies apres une analyse, il faut relancer l'analyse.
L'application efface volontairement l'ancien plan pour eviter d'importer avec
des reglages qui ne correspondent plus a la liste affichee.


6. CREATION DES MISSIONS
------------------------

Les photos sont classees selon leur date de prise de vue. Deux photos restent
dans la meme mission si l'ecart avec la photo precedente ne depasse pas le seuil
configure.

Avec le seuil par defaut de 5 minutes, un ecart superieur a 5 minutes commence
une nouvelle mission.

Les dossiers sont nommes avec le debut de la mission :

    Mission_2026-08-06_09-14

Le nom original de chaque fichier DJI est conserve dans le dossier de mission.


7. DESTINATION DES PHOTOS
-------------------------

Les missions sont importees dans :

    ~/Desktop/Enseirb/Stage_3A/Kael/Projet_Drone_Gascogne/DJI_Photo_Importer/DronePhotos/

Exemple :

    DronePhotos/
      Mission_2026-08-06_09-14/
        DJI_0433.JPG
        DJI_0434.JPG

L'application ne convertit pas les images et ne modifie pas leur qualite.


8. HISTORIQUE ET DETECTION DES NOUVELLES PHOTOS
-----------------------------------------------

L'historique est enregistre dans :

    drone_sync.json

Il est cree uniquement lors du premier import reussi. Pour identifier une
photo, l'application combine plusieurs informations : identite du drone et du
volume, chemin relatif, taille, date de modification et date de prise de vue.
Elle ne se base pas uniquement sur le numero du fichier DJI.

L'historique memorise notamment :

- le nom et le chemin source ;
- la taille et les dates ;
- la date du transfert ;
- la mission ;
- la destination locale ;
- le checksum SHA-256 calcule pendant le transfert.

Le JSON est d'abord ecrit dans un fichier temporaire, synchronise sur le disque,
puis remplace atomiquement. Un arret pendant l'ecriture ne doit donc pas laisser
un fichier JSON partiellement ecrit.

Si un fichier importe est ensuite supprime de sa destination locale, il est de
nouveau propose lors de l'analyse suivante.


9. SECURITE ET REPRISE APRES INTERRUPTION
-----------------------------------------

Pour chaque photo selectionnee :

1. l'application verifie que la source n'a pas change depuis l'analyse ;
2. elle copie vers un fichier temporaire unique sur le Mac ;
3. elle calcule le checksum complet pendant la copie ;
4. elle relit et verifie la copie locale ;
5. elle place atomiquement le fichier sous son nom final ;
6. elle ajoute seulement ensuite la photo a l'historique.

Si le drone est debranche ou si l'application s'arrete, les photos deja
verifiees restent enregistrees. Les autres seront proposees au prochain scan.

Un verrou empeche deux instances de l'application d'importer simultanement vers
le meme dossier. Un fichier local different portant deja le meme nom n'est
jamais ecrase : l'import s'arrete avec une erreur claire.


10. DIAGNOSTIC ET JOURNAL TECHNIQUE
-----------------------------------

Les erreurs lisibles sont affichees dans l'interface. Les details techniques
sont conserves dans :

    logs/drone_importer.log

Erreurs courantes :

"Aucun drone DJI detecte"

- verifier que le drone est allume ;
- verifier que le cable transporte des donnees ;
- essayer un autre port USB.

"Le stockage du drone n'est pas accessible"

- attendre le montage dans le Finder ;
- verifier que le drone expose un stockage USB et pas uniquement MTP ;
- verifier la carte microSD si les photos y sont stockees.

"Le drone a ete deconnecte"

- rebrancher le drone ;
- relancer l'analyse ;
- recommencer l'import. Les fichiers deja valides ne seront pas recopies.


11. MODES TERMINAL POUR LE DIAGNOSTIC
-------------------------------------

Scanner et afficher toutes les missions sans copier de fichier :

    python3 drone_photo_importer.py --scan

Changer temporairement le seuil :

    python3 drone_photo_importer.py --scan --gap-minutes 10

Importer toutes les nouvelles missions sans interface :

    python3 drone_photo_importer.py --import-all

Cette derniere commande effectue de vraies copies et met a jour l'historique.


12. MODE HISTORIQUE CONSERVE
----------------------------

L'ancien script reste disponible et n'a pas ete remplace :

    python3 get_latest_drone_photo.py

Il recupere uniquement la derniere photo. Il reste utile pour un diagnostic
simple ou pour verifier rapidement l'acces USB au drone.


13. ARCHITECTURE DU PROJET
--------------------------

Les responsabilites sont separees :

- get_latest_drone_photo.py : detection USB et EXIF historiques reutilises ;
- drone_importer/core.py : modele DronePhoto et scan complet ;
- drone_importer/missions.py : regroupement chronologique pur ;
- drone_importer/history.py : historique JSON atomique ;
- drone_importer/transfer.py : copie, verification et reprise ;
- drone_importer/gui.py : interface PyQt5 et workers QThread ;
- drone_importer/main.py : modes terminal et lancement graphique ;
- tests/test_drone_importer.py : tests automatiques du coeur metier.

Cette separation permet d'ajouter plus tard une previsualisation, le renommage
des missions, une autre destination ou le lancement de WebODM sans melanger ces
fonctions avec l'acces au drone.


14. TESTS EFFECTUES
-------------------

Le scanner a ete teste avec le DJI Mini 5 Pro branche au Mac :

- 314 photos detectees ;
- 4 missions creees avec un seuil de 5 minutes ;
- dates EXIF lues correctement ;
- interface remplie par un worker QThread sans blocage.

Les tests automatiques verifient aussi :

- premier scan, second scan et ajout d'une nouvelle photo ;
- regroupement par missions ;
- copie exacte sans modification de la source ;
- historique partiel apres interruption ;
- refus d'une source modifiee apres le scan ;
- detection d'une corruption au milieu d'un fichier de meme taille.

Les 314 photos reelles du drone n'ont pas ete importees en masse pendant les
tests de developpement. L'utilisateur garde le controle via les cases a cocher
et le bouton d'import.
