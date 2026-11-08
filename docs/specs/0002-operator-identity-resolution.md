# Résoudre une identité depuis la page opérateur

Current work-level confirmation and admission rules are in
[spec 0003](0003-work-identification-and-metadata.md). This earlier specification
still describes exact-edition decisions, corrections and their recovery.


Document de revue, en français. Date : 8 novembre 2026.
Statut : **périmètre et arbitrages validés par the operator le 8 novembre 2026 ; implémentation différée**.
the operator a validé la résolution depuis la page, avec preuves et choix de variante.
Le cycle applique les décisions ; aucun bouton « Publier » n'est ajouté.
Un transfert utilise retrait puis création. Une absence peut durer après un échec.
Une dérive ou un journal incertain arrête toutes les copies.
Une cible déjà publiée est refusée, sauf retour vers sa propre copie conforme.
Les corrections documentaires après la contre-revue ne sont pas contre-revues.
Voir la disposition des revues.
Suivi existant : `private-work-item`. Ce document ne lance aucune implémentation.
La liste des tâches a été établie après cette validation.

## Résultat attendu

Depuis la fiche d'un fichier, the operator peut confirmer une édition proposée.
Il peut aussi rechercher une autre édition ou saisir une identité justifiée.
Il voit les preuves avant de décider. Une preuve insuffisante laisse le cas en attente.
La décision est durable. Le cycle normal publie ensuite la variante admissible.
La page ne comporte aucun bouton « Publier ».

Le projet reste le BETA/LAB existant.
Réutiliser le catalogue YAML, son journal de décisions et le service HTTP local.
Ne pas ajouter de base de données, framework, compte, lecteur ou sauvegarde externe.
Conserver les originaux, les secrets, les journaux et les révisions de récupération.

Cette spec complète la [spec V1](0001-portable-comic-library.md).
Elle remplace seulement sa limite « revue en conversation » et la limite
« page en lecture seule » de son amendement de lab, après acceptation.
Les contrôles d'intégrité, d'identité et de publication restent obligatoires.
L'assemblage de fichiers, Kavita, le contrôle de pyLoad et la gestion générale
des tâches restent hors périmètre.

## Base vérifiée et travail nouveau

Les faits ci-dessous viennent du code local lu pour cette conception.
Ils décrivent les mécanismes disponibles, pas la réussite de la future extension.

| Mécanisme existant | Limite à traiter |
| --- | --- |
| `library/web.py` sert une projection échappée et des fiches. Il refuse les écritures HTTP. | Ajouter des actions bornées sur le cas courant. La projection ne devient pas une autorité. |
| `library/review.py` calcule un identifiant et une version de cas. `decide` contrôle un schéma fermé et déduplique `request_id`. | Ajouter le contrat de prévisualisation et les preuves manuelles structurées. Une preuve actuelle est seulement un dictionnaire non vide. |
| `library/catalog.py` écrit une décision immuable avant son effet sur l'index. Il contrôle la révision et permet le rejeu. | Préserver ce journal. Ajouter des décisions atomiques pour identité et sélection. Définir la compatibilité des anciennes décisions. |
| `identify` exige titre, langue, éditeur et preuve. Il ne sélectionne aucune variante. | Présenter et enregistrer un choix de variante explicite. Ne pas fabriquer les champs absents. |
| Les variantes existantes interdisent deux revendications d'édition pour le même asset. Une édition existante ne change pas silencieusement d'ouvrage. | Une correction d'attribution doit remplacer une revendication active et conserver son historique. Elle ne peut pas contourner le contrôle actuel. |
| `library/automatic.py` conserve les issues `pending`, `unavailable` et `published`. Il ne les réexamine pas après une décision web. | Une nouvelle décision applicable doit rendre son cas éligible sans relancer OCR et recherche déjà acquis. |
| `Publisher.publish` contrôle les origines et tous les fichiers gérés. Il propose un remplacement explicite avec récupération. | Le cycle n'autorise actuellement aucun remplacement. Relier une correction validée à une opération précise. Ne jamais activer `replace` de façon générale. |
| `ops/library_service.py` sert HTTP dans un fil et lance un cycle enfant séquentiel. Le cycle dispose d'un verrou non bloquant. | Garder HTTP réactif. Coordonner les décisions avec le cycle sans verrou pendant réseau, hachage ou transformation. |
| `Providers` borne les réponses à 2 Mio, le délai à 15 s et les appels à un budget. Il met les réponses en cache. | La recherche web doit avoir son propre budget partagé, borné et visible. Elle ne doit pas lancer plusieurs travailleurs réseau concurrents. |

## Parcours opérateur

La fiche conserve le téléchargement, les contrôles, l'OCR et les motifs actuels.
Elle ajoute un bloc « Résoudre l'identité » pour un asset validé et traçable.
Un transfert incomplet ou un fichier rejeté affiche son blocage sans admission.
Plusieurs origines identiques ouvrent le même cas d'asset.

Le bloc compare les valeurs confirmées, les candidats et les contradictions.
Il montre titre, auteur, série, tome, langue, éditeur, date, révision et ISBN,
avec leur source. Il montre l'ouvrage, l'édition et la variante visés.
Chaque champ absent porte « inconnu ». Les champs ne sont pas préremplis par
déduction du nom de fichier ou du titre Bookys.

the operator peut :

- Choisir une édition déjà proposée et examiner ses preuves.
- Rechercher par titre, auteur ou ISBN, puis choisir une édition précise.
- Saisir une autre identité avec sa référence et ses preuves.
- Laisser le cas en attente sans confirmer une identité incomplète.

Le serveur reconstruit les valeurs à partir des références et preuves retenues.
Il ne fait pas confiance à une fiche ou à des champs cachés envoyés par le navigateur.
Les textes externes restent échappés et bornés. Aucun texte source ne peut
choisir une action, un identifiant de décision ou un chemin.

## Preuve manuelle et conservation

Une décision exige une preuve lisible qui relie le fichier à l'édition.
Une case « confiance » ou une note « vérifié » ne suffit pas.

| Preuve possible | Données conservées |
| --- | --- |
| Page du fichier lisible | Empreinte de l'asset, numéro et nom de page, image locale bornée, empreinte de l'image et mentions lues. |
| ISBN visible dans le fichier | ISBN valide, page où il est visible, notice d'édition retenue et concordances de contenu. L'ISBN saisi seul ne prouve pas sa présence dans le fichier. |
| Référence bibliographique précise | Fournisseur et identifiant de notice, URL qualifiée, extrait local lisible et daté, champs d'édition retenus et lien de concordance avec le fichier. |
| Mentions saisies par the operator | Valeur transcrite, page ou référence justificative, auteur et date. Les mentions permettent de comprendre la décision après disparition de la source. |

Chaque champ qui distingue cette édition référence au moins une page de l'asset,
son empreinte et une mention transcrite. Une même page peut justifier plusieurs
champs. Une notice précise peut compléter cette concordance. Le serveur contrôle
la structure, les références et les empreintes. the operator juge la lecture et le sens.

Le dossier local conserve les pièces nécessaires à cette décision et leurs
empreintes. Le journal immuable les référence. Le cache expirant des fournisseurs
ne constitue pas ce dossier. Conserver les pièces avant d'enregistrer la décision.
Une pièce manquante ou modifiée bloque l'enregistrement ou le rejeu concerné.
Le catalogue et la fiche affichent les données utiles sans exposer les corps
bruts, les chemins privés ou les secrets sur HTTP.

La preuve doit distinguer l'édition. Un titre et une couverture ressemblante
ne suffisent pas. Une notice exacte ne prouve pas seule que le scan en provient.
Une contradiction importante doit recevoir une réponse explicite avec sa preuve.
Une confirmation n'efface pas les assertions contradictoires.
Un conflit nouveau après la décision reste visible et bloque la prochaine copie.
Il ne retire pas automatiquement une copie déjà exposée.

Titre, langue et éditeur restent obligatoires, comme V1. Date, révision, auteur
ou tome absents restent inconnus. Leur absence bloque si elle empêche de distinguer
l'ouvrage ou l'édition. Ne pas enregistrer « inconnu » comme valeur réelle.

Bornes proposées : quatre pages justificatives par décision, choisies dans le
fichier validé, y compris hors de la fenêtre OCR. Produire une image PNG à 200 dpi
au maximum et limitée à 2 Mio. Réduire la résolution si nécessaire ; afficher la
résolution obtenue. the operator doit pouvoir lire les mentions à taille réelle.
Sinon, signaler la limite sans accepter cette preuve. Conserver les numéros,
noms et empreintes des pages sources. Ne modifier aucun original.
Une décision conserve au plus deux notices qualifiées de 2 Mio chacune et
4 000 caractères de justification. Son dossier complet est limité à 13 Mio.
Les métadonnées et le manifeste disposent ainsi d'une réserve de 1 Mio.
Une seule prévisualisation active existe par cas, avec pièces adressées par
empreinte. Son remplacement réutilise les pièces identiques. Une réserve globale
de 64 Mio borne les pièces temporaires non référencées par une décision.
Pour libérer ce quota, invalider la plus ancienne prévisualisation non référencée,
puis retirer ses seules pièces temporaires sans autre référence. Une confirmation
de sa version est périmée. Répéter si nécessaire ; refuser si aucune éviction sûre
ne suffit. Ne purger aucune pièce décidée, aucun journal ni récupération.
Aucun téléversement ni chemin de fichier libre n'est nécessaire.
Une identité sans notice externe reste possible si les pages du fichier
distinguent réellement l'édition. Une notice seule sans concordance au fichier
reste insuffisante. Ces bornes sont des choix proposés à valider avec cette spec.

## Recherche bornée

Réutiliser Open Library pour découvrir des candidats et résoudre une référence
d'édition ou un ISBN. Réutiliser la route BnF existante pour un ISBN exact.
Une fiche de série BDTheque ou MangaDex ne devient pas une preuve d'édition.
Ne pas qualifier un nouveau fournisseur par le seul ajout de son domaine.
Les formats de réponses et les règles d'accès restent ceux des adaptateurs
présents, jusqu'à qualification explicite d'un changement.

Une recherche explicite utilise au plus 12 requêtes réseau, séquentielles,
dont au plus une découverte et trois résolutions de candidats.
Les appels auteur et les redirections comptent dans cette limite.
Une seule recherche réseau opérateur est active pour le service.
Le service impose aussi une enveloppe commune de 50 requêtes réseau par cycle
de service pour recherche opérateur et enrichissement automatique. Cette enveloppe
reste commune pendant le cycle et les 300 secondes qui suivent sa fin.
Une recherche ne la réinitialise pas. Les appels réseau de ces deux parcours
restent séquentiels ; le contrôleur arbitre leur accès sans verrou de catalogue.
Le solde, les limites atteintes et les indisponibilités sont visibles.
Une recherche refusée faute de budget ne consomme pas une tentative d'identité.
Une réponse de cache valide n'effectue pas une requête réseau.

Seul un POST explicite lance la recherche. HTTP reste réactif.
Une frappe, un rafraîchissement ou une fiche ne lance aucun appel.
Une recherche interrompue ne redémarre pas seule. Les résultats datés restent
des propositions et ne remplacent aucune décision.

Les URL réseau sont construites par les adaptateurs qualifiés.
Une URL libre saisie par the operator reste une référence textuelle ; le serveur
ne la télécharge pas. Un fournisseur invalide, une redirection hors origine,
une réponse inattendue ou un échec de solveur arrête cette recherche.
Ne visiter aucun hébergeur de téléchargement. Ne transmettre aucun secret.

## Choix de variante

La prévisualisation montre le fichier courant et les variantes connues de
l'édition : format source, taille, nombre de pages, origines et sélection actuelle.
Ces données ne constituent pas un classement automatique de qualité.
the operator choisit explicitement « sélectionner ce fichier » ou « conserver la
sélection actuelle ». L'absence de sélection reste visible et bloque la publication.
Le formulaire présente aussi ce choix quand il existe une seule variante.
Si cette variante est la seule admissible, le formulaire peut la présélectionner.
L'aperçu montre cette sélection et la confirmation l'enregistre explicitement.
Avec plusieurs variantes, aucun choix nouveau n'est implicite.

L'identité et la sélection forment une décision atomique. Une nouvelle variante
ne remplace aucune sélection sans ce choix. Les variantes non choisies et leurs
origines restent conservées. Confirmer un asset ne confirme pas un autre asset.

## Prévisualisation, enregistrement et application

La prévisualisation n'écrit ni décision ni publication.
Elle peut conserver ses propres pièces locales identifiées et bornées.
Elle calcule les effets depuis un instantané du catalogue et du journal.
Elle rend une version de prévisualisation liée au cas, à ses entrées, au contenu
exact proposé et à toutes les entités affectées.
La prévisualisation reste valable tant que ces dépendances restent identiques.
Elle ne dépend pas d'un délai arbitraire. Les dépendances forment une projection
fermée : empreinte/format/intégrité/pages de l'asset, identifiants pyLoad et chemins
des origines retenues, revendications actives, champs bibliographiques exportés,
propriétaires et sélections affectés, décisions applicables, empreintes des pièces,
correspondances notice ou ISBN vers édition, cible et manifeste des publications.
Inclure aussi `assertions`, `identity_result`, `contested` et `disposition`.
Figer `resolution_inputs` dans cette prévisualisation. Une assertion nouvelle ne
peut pas être réputée résolue sans avoir été présentée. La version du cas dérive
de cette même projection, et non du dictionnaire complet de l'asset.
Exclure `automation`, `observed_at`, dates de consultation et compteurs de tentative.
Un autre ouvrage sans relation avec cet effet n'en fait pas partie. Les exports
stables excluent aussi ces champs volatils et les pièces privées de revendication.
Toute modification de formulaire invalide la prévisualisation affichée.

the operator voit avant confirmation :

- Les anciennes et nouvelles valeurs et leurs preuves.
- La sélection avant et après, avec les identifiants stables concernés.
- Les autres assets, éditions ou ouvrages affectés par une donnée partagée.
- L'effet prévu : attente, première copie, copie inchangée ou remplacement.
- Pour une copie exposée : cible exacte, révision publiée et fichiers remplacés.
- Le retour possible et la révision de récupération conservée.
- Les contrôles qui restent à refaire au moment de l'application.

La prévisualisation annonce les changements de contenu logique, y compris
ComicInfo.xml, fiche et couverture. Une transformation non encore exécutée
ne dispose pas d'une empreinte finale prouvée. Ne pas l'afficher comme calculée.
L'application ne peut ajouter un effet ou une cible absents de la prévisualisation.

Le bouton « Confirmer cette décision » envoie un identifiant de requête unique,
l'identifiant du cas, sa version, la version de prévisualisation et son contenu
fermé. Le serveur contrôle les types, les tailles, les références, les preuves,
la sélection, les propriétaires et le journal. Un champ inconnu est refusé.
Les mêmes règles s'appliquent aux commandes locales qui utilisent ce mécanisme.

Sous un verrou court, le serveur lit un instantané cohérent et le journal,
projette en mémoire les décisions déjà enregistrées et vérifie les dépendances.
Il écrit seulement les pièces et la décision immuable. HTTP ne modifie jamais
`library.yaml`. Le cycle reste l'unique écrivain de ce catalogue, y compris pour
les décisions locales. Tous ses chemins respectent le verrou de cycle existant.
Le démarrage d'une opération et l'ajout d'une décision partagent une courte
section de contrôle par les verrous existants. Une opération en cours réserve
ses assets et éditions. Une confirmation qui les touche attend une nouvelle
prévisualisation après sa fin. Une décision déjà enregistrée est prise en compte
avant de réserver l'opération suivante. Aucun verrou court ne couvre une copie.
La décision conserve auteur `flowcool`, date, contenu accepté, preuves,
prévisualisation, empreintes avant et après son effet et décisions remplacées.
L'identité du compte est une convention du lab ; le LAN ne prouve pas l'auteur humain.

| Résultat | Effet |
| --- | --- |
| Enregistrée | Décision durable. Projection et copie au cycle normal annoncées séparément. |
| Périmée | Entrées affectées différentes. Aucun nouvel effet. Nouvelle prévisualisation exigée. |
| Refusée | Schéma, preuve ou propriétaire invalide. Aucun nouvel effet. Motif lisible. |
| Déjà enregistrée | Même identifiant et contenu : retourner la même décision et son état réel. |
| Identifiant réutilisé | Contenu différent pour le même identifiant : refus, sans deuxième décision. |
| Résultat incertain | Relire le journal par identifiant avant toute nouvelle tentative. Ne pas inventer une nouvelle requête pour contourner l'incertitude. |

Après ajout durable au journal, « refusée » n'est plus un résultat possible.
Un échec de projection ou de copie donne « enregistrée, application bloquée ».
La fiche et les prévisualisations calculent en mémoire catalogue plus décisions
non projetées. Elles distinguent toujours cette cible de la copie réellement exposée.

Une révision globale sans changement des entrées affectées ne doit pas rendre
toutes les prévisualisations inutilisables à chaque cycle. Le contrôle final
utilise les versions des dépendances exactes. L'écriture du cycle conserve son
contrôle de révision optimiste ; aucune confirmation HTTP ne peut changer cette
révision au milieu d'une transformation ou d'un échange de dossiers.
La version actuelle de `cases_for` inclut tous les ouvrages ; son périmètre doit
être ajusté sans perdre les contrôles de propriété et de fusion.

Si le processus s'arrête après le journal mais avant l'index, le rejeu applique
la même décision. Une pièce ou une entité manquante suspend ce rejeu.
Une ancienne décision reste lisible et rejouable selon son schéma d'origine.
Ne pas modifier les fichiers de décisions historiques pour les adapter.
Le rejeu reconstruit en mémoire les effets bibliographiques dans l'ordre du journal,
puis contrôle les revendications actives finales. Il ne réexécute pas un ancien
contrôle d'admission contre une revendication remplacée plus tard. Le journal porte
identité, sélection et transfert ; les opérations de copie portent publication et
artefacts. Rejouer une décision ne déplace aucun fichier et ne réévalue pas sa preuve
contre une nouvelle version de cas.

## Application par le cycle normal

La décision est le signal durable à consommer. La page n'introduit pas une
deuxième file canonique. Le cycle projette son état dans le catalogue existant.
Il distingue « décision enregistrée », « application en attente », « appliquée »
et « application bloquée ». Il affiche le motif et l'opération concernée.

Le cycle reconnaît une nouvelle décision même si l'asset était `pending`,
`published` ou `unavailable`. Il réutilise les contrôles et preuves inchangés.
Une résolution manuelle ne relance pas automatiquement OCR et recherche.
Elle n'efface pas une erreur de copie ni un journal incertain.
Au démarrage du cycle, réconcilier avant toute écriture du catalogue, y compris
réception, activation et projection. Une opération interrompue retrouve ainsi sa
révision attendue. Après réconciliation, projeter les décisions puis traiter leurs effets locaux avant
l'enrichissement réseau, dans la limite de huit assets déjà fixée. Un budget
fournisseur nul n'empêche pas cette application. La décision déjà projetée n'est
pas une nouvelle décision lors du `replay()` de publication.

Une première copie exige toujours : fichier validé, origine présente et conforme,
identité confirmée, édition déterminée, variante sélectionnée, aucune revendication
active contradictoire, aucune exclusion ou attente opérateur, capacité suffisante.
Il ne doit exister aucune décision en attente ou bloquée sur cet asset ou cette
édition, sauf pour le rôle qu'elle autorise. Cette porte s'applique aussi aux
commandes CLI et aux publications ordinaires, sans contournement par `--asset`.
Une décision dont l'effet annoncé est « attente » devient terminale dès sa
projection ; elle ne réserve pas indéfiniment l'asset ou l'édition.
Une décision d'identité ne lève pas silencieusement une exclusion ou un report.
La prévisualisation indique ce blocage et laisse sa levée explicite hors formulaire.

Avant copie, le cycle compare la projection fermée à l'empreinte attendue **après**
la décision, puis refait les contrôles physiques. Il ne compare pas l'identité
nouvellement décidée à son ancienne valeur. Une différence métier bloque ce cas
avant toute opération de copie. Elle produit un résultat immuable dans le même
journal, lié à la décision. Les autres cas peuvent continuer à ce stade.
La publication n'est annoncée qu'après validation de son journal et du catalogue.
Une copie déjà conforme ne crée pas une nouvelle opération.
Chaque opération porte `decision_id`. Une décision autorise une seule opération
validée par rôle annoncé. Une correction ordinaire a un rôle ; un transfert publié
a les rôles retrait et création. « Appliquée » se déduit de ces opérations, même
après crash avant rafraîchissement de la page. Une absence de changement utilise
un résultat immuable « appliquée, copie inchangée », sans opération de remplacement.
Pour plusieurs rôles, la décision décrit la projection attendue après son effet
bibliographique et après chaque rôle déjà validé. Ses propres effets ne rendent
pas le rôle suivant périmé. Les changements étrangers à ces effets le bloquent.

Une seule correction non appliquée existe par asset et par édition touchée.
Pour changer une décision bloquée, une nouvelle prévisualisation doit nommer
explicitement la décision remplacée et partir de la copie réellement exposée.
Avant confirmation, toute opération commencée doit être réconciliée. La nouvelle
décision annule l'autorisation non consommée précédente, sans supprimer son histoire.
Le rejeu conserve ce remplacement ; il ne remet pas l'ancienne correction en attente.

Une dérive physique, un journal de copie incertain ou un rejeu impossible arrête
**toutes les copies**, comme le mécanisme actuel. La page reste lisible. Ne pas
promettre un isolement par dossier qui exigerait une refonte de la réconciliation.

## Correction et copie déjà exposée

La fiche montre l'identité décidée et l'identité de la copie réellement exposée
tant que leur mise à jour n'est pas achevée.

Pour un changement de métadonnées ou de variante dans la même édition, la
confirmation autorise seulement le remplacement décrit par la prévisualisation.
Le cycle utilise les contrôles et la récupération de `Publisher` pour cette
opération. Le simple fait qu'un export diffère ne donne aucune autorisation.
Les anciennes fiches, couvertures et archives gérées sont vérifiées avant échange.
L'ancienne révision reste hors exposition, sans purge automatique.

Une édition existante conserve son ouvrage propriétaire. Le choix d'une notice
externe ne fusionne pas des ouvrages et ne déplace pas une édition existante.
Les identifiants locaux restent stables. Les références externes sont des preuves.
Un identifiant déjà lié à un autre ouvrage est un conflit, jamais une permission
d'écraser son enregistrement.

Pour une édition déjà connue, the operator choisit l'entité locale visible.
Le serveur conserve son identifiant et son ouvrage propriétaire. Ce rattachement
ajoute la preuve à la revendication de l'asset ; il ne réécrit ni l'ouvrage ni
l'édition, même si d'autres variantes sont déjà publiées. Une correction de leurs
champs partagés reste une action explicite avec son propre effet prévisualisé.
Les exports d'édition contiennent un résumé bibliographique stable, sans pièces
de revendication ni chemins privés. Une nouvelle
référence externe ne crée pas un doublon si elle désigne cette même édition.
Pour une édition nouvelle, le serveur attribue des identifiants locaux stables
avant la prévisualisation. Il présente le rattachement à un ouvrage connu ou
la création d'un ouvrage. the operator ne saisit pas d'identifiant technique arbitraire.
Le contrôle des candidats à réutiliser porte sur les preuves, jamais le titre seul.
Une équivalence incertaine reste en attente ; aucune fusion n'est implicite.
La correspondance référence de notice ou ISBN vers édition fait partie des
dépendances contrôlées à la confirmation, y compris les décisions non projetées.
Deux onglets ne peuvent donc pas créer deux éditions pour la même référence :
la première confirmation rend la seconde prévisualisation périmée.
Un ouvrage marqué comme fusionné ne peut pas redevenir une cible active.
La prévisualisation montre son propriétaire courant ou refuse la référence ambiguë.

Une mauvaise attribution à une autre édition exige un transfert explicite de
la revendication de l'asset. L'ancienne variante reste historique et inactive.
Les autres variantes de l'ancienne édition restent intactes. Une variante
historique ne bloque pas seule une nouvelle attribution active.
Une deuxième revendication active indépendante reste bloquante.
Le rejeu respecte les anciennes décisions dans l'ordre puis le transfert.
Le transfert nomme la revendication active remplacée. Il n'a pas de nouvel effet
s'il est déjà projeté. Un retour A→B→A réactive la variante stable de A ; il ne
crée pas un nouvel identifiant pour les mêmes asset et édition.

Si une copie exposée est concernée par ce transfert, la prévisualisation doit
décrire aussi le devenir de son ancien dossier et de son ancienne sélection.
Le mécanisme actuel de remplacement ne transfère pas la propriété entre éditions.
Pour ce transfert, exiger « sélectionner ce fichier » dans l'édition cible.
« Conserver la sélection actuelle » est refusé avant décision si une copie exposée
doit être transférée. Une édition cible déjà publiée est aussi refusée. Sa copie
reste intacte ; la mauvaise attribution reste exposée jusqu'à un retrait distinct.
Exception : un retour vers l'édition qui détient déjà ce même asset est autorisé
si son manifeste est conforme. Le résultat est « appliquée, copie inchangée » ;
l'autorisation de la correction remplacée est annulée.

Proposition pour le lab : utiliser le retrait existant puis une création normale.
La décision contient l'ancienne et la nouvelle édition, l'ancienne publication,
la cible nouvelle, les sélections et les deux effets autorisés dans cet ordre.
Le cycle projette le transfert de revendication et de sélection, puis exécute
`withdraw` sur la publication encore détenue par l'ancienne édition. Son journal
et son catalogue sont validés avant d'appeler la création dans la nouvelle édition.
Chaque opération porte la décision et son rôle « retrait » ou « création » ;
chaque rôle ne peut avoir qu'une opération validée. Le rejeu conserve les références
de publication jusqu'au retrait validé. Aucune édition ne perd son dossier en index
avant que ce retrait soit prouvé. Ne créer aucun journal transactionnel supplémentaire.
Avant le retrait, vérifier aussi les portes de création : origine présente et
conforme, capacité suffisante, cible libre, outils et conversion admissibles.
Exécuter les contrôles déterministes de conversion sans retirer l'ancienne copie.
Un résultat indéterminé bloque avant retrait. Ces contrôles ne garantissent pas
l'absence d'une panne ultérieure ; cette absence éventuelle reste visible.

La prévisualisation annonce une absence possible de la bibliothèque entre retrait
et création. Si la création échoue, cette absence dure jusqu'à résolution.
La page montre le retrait accompli, la cause du blocage et la révision récupérable.
Elle n'annonce pas la correction comme appliquée tant que la création n'est pas
validée. Conserver l'ancienne édition, ses autres variantes et sa récupération.
Le journal de décision retire l'ancienne sélection seulement si elle désignait
l'asset transféré. Ne choisir aucune autre variante automatiquement.

Après crash, réconcilier d'abord le retrait ou la création avec leur journal
existant. Si le retrait est validé sans création commencée, la création autorisée
reste à effectuer. Si une création a échoué, conserver son résultat bloqué sans
renvoi automatique. Une nouvelle correction peut ensuite la remplacer.
Le retour à l'ancienne copie exige une nouvelle décision vers l'ancienne identité
avec sa sélection, puis `restore` de la révision de retrait vérifiée. `restore`
seul ne corrige pas les métadonnées. Ce retour fait aussi l'objet d'une prévisualisation.
Cette décision porte le rôle « restauration », exécuté par le cycle à la place
d'une création, y compris pour un retour dans la même édition. Elle nomme la
révision exacte de récupération. Le cycle vérifie son manifeste, la variante
réactivée et l'origine conservée conforme à l'empreinte source. Une origine absente
ou modifiée bloque le retour ; ne pas promettre que la seule récupération la remplace.

Si les octets de l'artefact sont identiques dans les deux éditions, le commit
de création transfère sa revendication active seulement si la décision et le
retrait validé le permettent. L'historique des anciennes éditions est conservé.
Une association historique ne constitue pas une revendication active concurrente.
Un autre propriétaire actif reste bloquant.

Les fusions ou séparations d'ouvrages déjà disponibles en CLI restent des décisions
distinctes. La page ne les déduit jamais d'un titre ou d'une recherche.
Un changement partagé qui toucherait plusieurs copies exige une prévisualisation
de tout cet effet. Si l'opération ne peut pas le garantir, elle est refusée avant
l'écriture de la décision, avec la limite explicite.

Après échec de transformation ou de remplacement, conserver l'ancienne copie.
Après échange incertain, réconcilier le journal avant toute reprise.
Une dérive arrête les copies. Le retour exige une nouvelle décision et une
opération qui vérifie la récupération. Ne retirer aucune décision du journal.

## Écritures HTTP dans le lab LAN

Conserver l'adresse LAN actuelle. Les lectures restent bornées et sans recherche.
Les actions utilisent des POST explicites : recherche, prévisualisation, confirmation.
Ne créer aucune route de commande, chemin libre, retrait général ou exécution shell.

Le serveur vérifie `Host` contre les origines locales configurées, y compris
sur la route qui fournit le jeton. Chaque POST exige une origine navigateur
exactement autorisée et un jeton anti-CSRF imprévisible obtenu sur cette origine.
Un en-tête `Origin` absent, nul ou étranger est refusé pour ces routes navigateur.
Le jeton reste en mémoire du navigateur et du service, jamais dans une URL.
Un redémarrage invalide les jetons ; il ne perd aucune décision enregistrée.

Refuser les types de contenu inattendus, les corps trop grands et les champs
supplémentaires. Ne pas accepter de formulaire POST simple depuis un autre site.
Ne pas activer CORS. Refuser l'intégration de la page dans un cadre externe.
Échapper tous les textes et construire le DOM avec des valeurs textuelles.
Un lien bibliographique ne transmet aucun secret ni jeton.

Ces contrôles limitent les écritures déclenchées par un autre site et le changement
malveillant de résolution DNS. Ils ne constituent pas une authentification.
Toute personne qui atteint directement ce service de lab peut agir comme opérateur.
Un besoin d'accès partagé ou hors LAN impose une décision de périmètre distincte.

## Preuves d'acceptation

Les essais utilisent un catalogue, une réception et des journaux isolés.
Les fournisseurs utilisent des réponses enregistrées ou un transport de test.
Aucun essai ne visite un hébergeur ni n'utilise le dossier partagé pour écrire.

| Cas | Résultat exigé |
| --- | --- |
| Candidat exact et preuve lisible | Prévisualisation complète, décision durable, puis copie au cycle normal si toutes les portes passent. |
| Aucun candidat correct | Recherche bornée ou saisie d'une autre identité ; aucune acceptation forcée. |
| Référence exacte sans lien au fichier | Confirmation refusée ou cas laissé en attente. |
| Champ facultatif absent | Valeur inconnue conservée ; aucune invention dans ComicInfo ou la fiche. |
| Édition non distinguable ou contradiction non résolue | Attente, sans nouvelle copie. |
| Deux variantes pour une édition | Choix explicite ; les deux variantes restent traçables. |
| Confirmation sans sélection | Identité enregistrée, motif de publication bloquée visible. |
| Cas pending avant décision | Le cycle consomme la décision sans nouvelle recherche et publie si admissible. |
| Exclusion antérieure | Confirmation d'identité ne la retire pas. |
| Double clic et rejeu après coupure | Une seule décision ; même réponse par identifiant ; aucun doublon de copie. |
| Même identifiant, contenu différent | Refus. |
| Cas ou pièce changés après prévisualisation | Périmé ou bloqué, sans nouvel effet. |
| Mise à jour indépendante du catalogue | Pas de faux périmé si les dépendances visées restent identiques. |
| Correction d'une copie publiée | Effet exact visible avant confirmation ; remplacement dédié ; ancienne révision conservée. |
| Changement de variante publié | Seul le choix confirmé est appliqué ; l'ancien asset et ses origines restent conservés. |
| Correction vers une édition possédée par un autre ouvrage | Conflit visible ; aucune modification de son propriétaire. |
| Transfert entre éditions avec historique | Une seule revendication active ; historique intact ; aucun dossier orphelin après reprise. |
| Fusion antérieure puis correction | Propriétaire courant et dépendances contrôlés ; aucun ouvrage fusionné ressuscité silencieusement. |
| Cible déjà publiée ou effet partagé non pris en charge | Refus avant décision ; aucune mise à jour partielle. |
| Source externe disparue | Pièces locales lisibles et décision compréhensible. |
| Journal absent, pièce altérée ou dérive publiée | Application bloquée ; preuves conservées. |
| Crash avant/après journal, index ou échange de dossier | Rejeu et réconciliation cohérents ; copie ancienne ou nouvelle complète ; aucune réussite annoncée sans preuve. |
| Budget épuisé ou fournisseur indisponible | Motif visible ; recherche arrêtée ; aucun changement d'identité. |
| POST depuis une autre origine, jeton absent ou Host étranger | Refus sans décision ni appel fournisseur. |
| Texte source malveillant | Texte affiché comme donnée ; aucune action ou exécution. |
| Confirmation pendant une copie sans rapport | Aucun écrivain HTTP du catalogue ; ni échec de révision ni décision durable annoncée refusée. |
| Confirmation sur une édition réservée par une opération | Aucune décision ajoutée ; nouvelle prévisualisation après fin de l'opération. |
| Rejeu A→B→A et ancienne sélection | Une seule revendication active finale, sélection conforme, variante A réactivée sans nouvel identifiant. |
| Correction bloquée D1 puis correction D2 | D2 nomme D1, part de la copie réelle et invalide son autorisation non consommée. |
| Variante B rattachée à une édition publiée avec A | Aucun champ partagé ni fiche de A changé par le seul rattachement. |
| Budget réseau nul après confirmation | Décision locale traitée avant enrichissement ; aucune attente due au fournisseur. |
| Deux onglets créent la même notice | Une édition créée ; seconde prévisualisation périmée. |
| Transfert avec artefact identique | Historique conservé ; propriété active transférée seulement après retrait validé. |
| Quota de prévisualisation ou image illisible | Préparation refusée avec motif ; aucune pièce décidée retirée. |

## Choix proposés et qualification technique

La validation porte aussi sur les bornes de preuve, le budget commun, la sélection
explicite et les trois arbitrages suivants : retrait puis création avec absence
possible jusqu'au succès ; arrêt global des copies sur dérive ou journal incertain ;
refus de transfert vers une édition déjà publiée. Les limites des corrections
partagées sont explicites.
Ces choix produit ne sont pas délégués à l'implémentation.

Les mécanismes suivants restent à qualifier. Leurs API internes exactes ne
constituent pas une décision produit supplémentaire :

- Lecture isolée des pages demandées, dossier de preuve immuable et bornes
  effectives. La couverture et la fenêtre OCR actuelles ne couvrent pas ce parcours.
- Enchaînement du retrait et de la création avec les journaux existants, rôles
  consommés une fois et retour prévisualisé depuis la récupération.
- Projection compatible des décisions et revendications historiques. Le schéma
  actuel ne distingue pas variante active et variante remplacée. Le rejeu doit
  fonctionner depuis un ancien instantané, y compris après fusion d'ouvrages.
- Budget commun entre le service et son processus de cycle, avec comptage des
  redirections. Le budget `Providers` actuel reste local à un objet Python.
- Contrôle des dépendances de prévisualisation avant effet, malgré les cycles,
  onglets et confirmations concurrentes. Le contrôle actuel de cas est trop large.

Le retour de la future extension conserve les décisions et leurs pièces.
Désactiver les routes d'écriture arrête les nouvelles confirmations.
Une décision déjà enregistrée reste traçable ; son application ne disparaît pas
par le retrait de l'interface. Le code de retour doit comprendre le schéma écrit,
ou suspendre l'application jusqu'à reprise avec une version compatible.
