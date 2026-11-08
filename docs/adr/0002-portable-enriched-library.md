# Bibliothèque enrichie indépendante du lecteur

Document personnel de revue, en français. Date : 8 novembre 2026.
Statut : accepté par the operator le 8 novembre 2026 après revue critique Claude.
La [spec V1](../specs/0001-portable-comic-library.md) décrit la cible. Rien n'est déployé.

Bookys fournit des catégories trop larges pour classer les genres. Un paquet pyLoad
peut contenir plusieurs ouvrages ou plusieurs miroirs. Le même pyLoad reçoit aussi
des fichiers Lunii. Utiliser directement ses dossiers comme bibliothèque mélangerait
des formats, des états de transfert et des identités différentes.

Nous conservons un catalogue local indépendant du lecteur. Il distingue ouvrage,
édition, variante, fichier et origine. Il conserve les assertions des sources et
les corrections manuelles prioritaires. Les fiches sont des exports de ce catalogue.
Le catalogue par URL Bookys existant reste un export de collecte séparé.

La réception conserve les originaux. La bibliothèque `/data/booksys/library/bd/`
reçoit des copies indépendantes des PDF, CBZ et CBR Booksys validés. Un fichier
identique n'y apparaît qu'une fois. Une seule variante de chaque édition est exposée
par défaut. Les variantes non choisies restent référencées dans la réception.
Les lots à décomposer, les cas incertains et les fichiers Lunii restent hors bibliothèque.
Une édition indéterminée reste en attente. La V1 admet seulement les représentations
complètes dans un fichier. Un même asset revendiqué pour deux identités bloque l'admission.

Ce choix permet de changer de lecteur et de reconstruire les exports. Il évite
qu'un ajout futur de métadonnées à la copie modifie l'original. Il coûte du stockage
et impose un journal de publication avec reprise vérifiée. Les copies ne constituent
pas une sauvegarde contre la perte du disque.
Le composant bibliothèque lit la réception sans la modifier. La conservation réelle
des origines dépend aussi de pyLoad ; ses traitements devront être vérifiés.
Un journal de décisions distinct permet de rejouer les corrections après restauration
de l'index. Un dossier n'est exposé qu'une fois son contenu préparé et vérifié.
Les mises à jour suivent aussi un journal. Elles remplacent le dossier complet
après contrôle de sa version et conservent l'ancienne révision pour le retour arrière.

Nous écartons les liens physiques, car leurs modifications touchent aussi l'original.
Nous différons le référentiel détenu par Kavita, car le lecteur final reste à choisir.
Nous différons aussi l'écriture dans les archives et la classification par LLM.
La V1 conserve les métadonnées hors archives, sans promettre leur import automatique
par un futur lecteur. Les connecteurs MetaKavita restent des candidats à qualifier.
