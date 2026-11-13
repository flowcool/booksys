# Booksys

[English](README.md)

Booksys collecte des pages Bookys et leurs liens de téléchargement.
Il transmet les liens à pyLoad et prépare des copies CBZ pour une bibliothèque
de bandes dessinées. Le code du projet est sous licence MIT.
Ce projet personnel ne garantit ni assistance ni délai de réponse.

## Architecture actuelle

PostgreSQL 18.6 ou ultérieur porte l'état opérationnel.
Une seule table `work_items` contient les travaux des cinq étapes.
Chaque étape dispose de son propre processus de traitement.

| Service | Rôle |
| --- | --- |
| `controller` | Sert la page web et les commandes HTTP dans son propre processus. |
| `discovery` | Détecte les travaux à créer et actualise les observations. |
| `collection` | Collecte les pages et les liens de la source. |
| `intake` | Examine les fichiers reçus et valide leur format. |
| `metadata` | Traite les métadonnées des ouvrages. |
| `copies` | Prépare les copies de bibliothèque. |
| `routing` | Traite le routage des téléchargements. |
| `retirement` | Réconcilie les paquets terminés avant leur retrait de pyLoad. |
| `postgres` | Conserve l'état, les politiques et les travaux. |
| `pyload` | Gère les comptes, la file de téléchargement et les fichiers reçus. |
| `byparr` | Fournit le navigateur utilisé pour les requêtes Bookys. |

La validation et la conversion lancent un sous-processus de décodage.
Ce processus possède des limites de mémoire, de temps CPU et de taille de fichier.
Le processus appelant impose aussi un délai et arrête le groupe de processus
si ce délai expire. Les PDF passent par `pikepdf` et Poppler.
La conversion extrait les images intégrées quand le chemin prévu le permet.
Sinon, elle produit une image JPEG de chaque page.

Voir les [rôles du runtime](docs/runtime.md) et
l'[architecture](docs/architecture.md).

## Exécuter la pile

[`compose.stack.yaml`](compose.stack.yaml) décrit la topologie complète.
Il exige une configuration propre à l'installation.
Il utilise une image applicative issue de `Dockerfile.stack`.
`Dockerfile.pyload` ajoute les contrôles de stockage au téléchargeur.
Ces deux constructions ne créent pas de paquet Booksys publié supplémentaire.

Préparer les éléments suivants avant le démarrage :

1. Choisir les images exactes et conserver leurs références précédentes.
   Fournir `BOOKSYS_APP_IMAGE`, `BOOKSYS_BYPARR_IMAGE`,
   `BOOKSYS_PYLOAD_IMAGE` et `BOOKSYS_POSTGRES_IMAGE`.
2. Préparer PostgreSQL, son schéma applicatif et sa configuration d'autorité.
   Fournir les fichiers DSN privés et le répertoire du socket.
   Les fichiers DSN doivent avoir le mode `0600`.
3. Préparer les répertoires locaux, les montages NAS qualifiés et leurs contrôles
   d'identité. Les répertoires montés doivent déjà exister.
   Préserver la configuration pyLoad, ses comptes et les téléchargements originaux.
4. Sélectionner le réseau externe existant avec `BOOKSYS_NETWORK`.
   Fournir le groupe de contrôle avec `BOOKSYS_CGROUP_PARENT`.
   Définir les adresses et noms HTTP attendus.
5. Activer les cinq étapes avec
   `BOOKSYS_WORK_STAGES=collection,intake,metadata,copies,routing`.
   Fournir les autres variables obligatoires indiquées dans le fichier Compose.

Conserver ces valeurs et les secrets hors de Git.
Deux délais acceptent des secondes entières strictement positives :

| Variable | Défaut | Usage |
| --- | --- | --- |
| `BOOKSYS_BYPARR_BUDGET` | 120 s | Budget de résolution, démarrage du navigateur inclus. Augmenter si une résolution valide dépasse le délai sur un hôte lent. Définir la même valeur sur le client de collecte et Byparr. Le timeout HTTP ajoute 20 s pour le nettoyage. |
| `BOOKSYS_DB_DEADLINE` | 30 s | Délai total par transaction PostgreSQL. Augmenter si des transactions valides atteignent `storage_busy`, après contrôle de la contention. Les délais explicites des appelants et les délais englobants plus courts restent prioritaires. |

Définir ces variables dans `environment` des services concernés du fichier stack.
Une valeur invalide bloque le démarrage avec une erreur qui nomme la variable.
Recréer les services concernés pour appliquer la valeur. Aucune construction
d'image n'est requise. Voir les [détails d'exploitation](docs/operations.md).

Les commandes suivantes supposent un fichier local `.env` préparé et validé :

```bash
docker compose --env-file .env -f compose.stack.yaml config --quiet
docker compose --env-file .env -f compose.stack.yaml config --services
```

Inspecter aussi les montages et les images de la configuration résolue.
Sa sortie peut contenir des valeurs privées.
Après validation de la cible, démarrer les services applicatifs :

```bash
docker compose --env-file .env -f compose.stack.yaml up -d
```

Cette commande suppose que PostgreSQL, pyLoad et Byparr sont déjà disponibles
sur le réseau choisi. Pour les gérer avec ce même fichier Compose,
activer explicitement le profil `retained-dependencies` :

```bash
docker compose --env-file .env -f compose.stack.yaml --profile retained-dependencies up -d
```

Ne pas créer une seconde instance du téléchargeur ou de la base par erreur.
Le profil `tools` donne accès aux commandes opérateur sur demande.
Le fichier [`compose.ghcr.example.yaml`](compose.ghcr.example.yaml)
montre les interfaces des images publiées. Il affiche leur aide par défaut.

## Vérifier la santé

Vérifier les services et leurs journaux :

```bash
docker compose --env-file .env -f compose.stack.yaml ps
docker compose --env-file .env -f compose.stack.yaml logs --since 10m controller collection intake metadata copies routing
```

[`ops/health.py`](ops/health.py) affiche l'âge de l'observation pyLoad,
les travaux par étape, les motifs d'attente, les conteneurs et les erreurs récentes.
Il lit l'état sans le modifier.
Ses chemins de laboratoire, son filtre de noms de conteneurs et son groupe
de contrôle sont encore fixes. Les adapter à l'installation avant usage.
Le script ne propose pas encore d'option pour les sélectionner.
Dans une copie adaptée et avec l'environnement Python installé :

```bash
uv run --no-sync python ops/health.py
```

Un processus actif ne prouve pas que les travaux progressent.
Vérifier aussi la fraîcheur des observations, les motifs d'attente et les erreurs.

## Stockage

| Emplacement logique | Contenu et accès |
| --- | --- |
| État technique local | PostgreSQL, configuration d'autorité, fichiers de travail et preuves. |
| Réception sur NAS | Originaux écrits par pyLoad et montés en lecture seule dans l'application. |
| Bibliothèque sur NAS | Copies CBZ gérées par Booksys. |
| Répertoires de fiches et couvertures | Fichiers générés, séparés des originaux reçus. |
| Pièces et entrées de lots | Fichiers conservés pour le traitement et la traçabilité. |

Les variables `BOOKSYS_LOCAL_ROOT`, `BOOKSYS_COLLECTOR_ROOT`,
`BOOKSYS_NAS_FILES_ROOT` et `BOOKSYS_NAS_LIBRARY_ROOT` sélectionnent ces espaces.
Compose définit leurs chemins dans les conteneurs.
Les fichiers historiques restent des entrées de migration ou des preuves.
PostgreSQL reste la seule autorité opérationnelle.
Conserver les sauvegardes et les journaux après une opération au résultat incertain.
Réconcilier ce résultat avant de répéter une écriture.

## Images et versions

Le jeu de publication contient trois images `linux/amd64` :

| Image | Interface |
| --- | --- |
| `ghcr.io/flowcool/booksys-worker` | Pipeline de collecte. |
| `ghcr.io/flowcool/booksys-library` | CLI de bibliothèque, modules du contrôleur et des étapes. |
| `ghcr.io/flowcool/booksys-byparr` | Adaptateur de session du navigateur. |

Vérifier la disponibilité réelle, le digest et la signature avant usage.
La documentation décrit une première préversion `v0.1.0`, avec le tag image
`0.1.0` et sans alias `latest`.
Une publication de sources ne démarre aucune installation.

Une GitHub Release publiée déclenche
[le workflow de publication](.github/workflows/release.yml).
Il vérifie le tag et la révision, construit les trois archives OCI,
puis exécute les sondes et les analyses de sécurité sur ces mêmes archives.
Il conserve l'inventaire, les notices, le SBOM et la provenance.
Le fichier `release/sources.lock.json` doit correspondre aux composants installés.
Les sources correspondantes sont assemblées avant la publication des images.
Le workflow publie ensuite les sources et les images, signe leurs digests
et vérifie le résultat. Un échec incertain impose une réconciliation.

Voir les [interfaces des images](docs/public-images.md),
la [publication et la récupération](docs/releases.md),
les [validations d'images](docs/image-validation.md) et
les [conditions de redistribution](docs/redistribution.md).
Les licences des dépendances restent distinctes de la licence MIT du projet.
Lire les [notices tierces](THIRD_PARTY_NOTICES.md).

## Contribuer

```bash
uv sync --locked --extra dev
uv run --no-sync python booksys.py --help
uv run --no-sync python -m library.cli --help
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync mypy booksys.py pipeline.py fiches.py library booksys_core ops/image_validation.py
```

La suite complète exige une base PostgreSQL 18.6 jetable, les outils natifs
d'archives et de PDF, ainsi que Node.
Définir `BOOKSYS_TEST_PG_DSN_FILE` avec un fichier privé de mode `0600`
qui sélectionne uniquement cette base de test.
Plusieurs tests échouent si cette variable manque.
Ne jamais utiliser une base partagée pour les tests.

Lire les [consignes de contribution](CONTRIBUTING.md),
les [contrôles CI](docs/contribution-ci.md) et
la [politique de sécurité](SECURITY.md).
Conserver les identifiants, les téléchargements réels et les preuves
opérationnelles hors de Git.
