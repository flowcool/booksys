# Bibliothèque enrichie indépendante du lecteur

Current operating scope: BETA/LAB.
The lab uses retained originals and journaled recovery, without an external
backup or extra account/deployment/GO. Production prerequisites are separate.

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
receives independent CBZ copies of validated Booksys PDF, CBZ and CBR files. Un fichier
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
Amendement accepté par the operator le 8 novembre 2026 : convertir les copies CBR en
CBZ et générer ComicInfo.xml dans tous les CBZ publiés depuis le catalogue validé.
CBR et CBZ peuvent tous deux contenir ComicInfo. ZIP permet une écriture ouverte
et plus simple à maintenir. Les images gardent leurs octets, noms et ordre.
Further amendment confirmed by the operator on 8 November 2026: uniform CBZ output
also applies to PDF sources. Preserve originals. Extract a complete page image
without re-encoding only when geometry and colour/annotation/transparency effects
are proved equivalent. Use qualified Poppler rendering for every other page.
Raster pages are PNG at 200 dpi, with visible CropBox and rotation applied.
Record the method, geometry, resolution and hashes per page. Rendering loses
PDF text, vectors, links and editability in the copy; the original retains them.
Uniform reader input is the accepted trade-off. Conversion/OCR does not permit
publication of an unidentified edition. PDF acceptance belongs to the open
extension in private-work-item, not the earlier closed archive delivery.
La fiche externe et le catalogue restent conservés. Le lecteur reste remplaçable.
La V1 journalise la transformation et distingue empreinte source et artefact.
La classification par LLM reste exclue. Les connecteurs restent à qualifier.
Références : [Kavita metadata](https://wiki.kavitareader.com/guides/metadata/general/),
[Kavita comics](https://wiki.kavitareader.com/guides/metadata/comics/),
[ComicTagger](https://github.com/comictagger/comictagger).
