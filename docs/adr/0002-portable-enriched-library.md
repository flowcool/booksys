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

## Accepted lab operation amendment — 8 November 2026

the operator confirmed automatic new-input processing and a read-only LAN page after
scope review. Reuse the existing catalogue and commands. One user service owns a
sequential cycle thread and a responsive stdlib HTTP server. Start each cycle
five minutes after completion; use one cycle lock, incremental validation and
bounded per-cycle work. Keep successful uncertain identities pending; retry only
temporary failures, three attempts with 300/900-second delays. Request-budget
deferral is scheduling, not a failed attempt. Preserve all identity gates.

The page is a compact derived product snapshot, not live pyLoad and not another
source of truth. It exposes transfer/intake/OCR/identity/publication status, reasons,
fiche links and age. Serve no actions, raw evidence, hoster links or filesystem
browser. This keeps the lab usable without a framework or another database.

Each origin has a detailed derived fiche, including pending inputs. Explicit
bounded fields preserve the distinction between confirmed identity, candidates,
assertions and overrides. Missing data stays unknown. Bibliographic links use a
source allowlist; local first-page images have explicit provenance. Vanilla JS
implements per-stage filters, search and column sorting without a framework.

## Proposed operator-resolution amendment — 8 November 2026

Status: scope and lab trade-offs accepted by the operator on 8 November 2026;
implementation deferred. Later internal documentary corrections have not received
an external approval. See
[spec 0002](../specs/0002-operator-identity-resolution.md).

Extend the existing LAN fiche with bounded identity decisions. Keep the local
catalogue and immutable decision ledger as the authority. Keep the normal cycle
as the only application path for publication. A confirmation records evidence,
identity and explicit variant selection; it is not a publication command.

Require a versioned effect preview before confirmation. Bind a published-copy
correction to its exact target, prior manifest and recovery operation. Preserve
prior decisions and recovery revisions. Never infer permission to replace from
a changed export. This adds write semantics to the accepted read-only page;
keeping conversation-only decisions would leave routine resolution outside the
operator view. A second database or general administration interface would add
state and maintenance without improving the existing lab boundary.

After the first Claude review, keep the cycle as the sole catalogue writer.
HTTP appends evidence and canonical decisions; views project pending decisions
in memory. Claim evidence belongs to the asset's assignment. Attaching another
variant does not rewrite shared edition or work metadata. Replay reduces the
decision history before checking the final active ownership.

Proposed lab trade-offs for the operator: transfer an exposed copy through the existing
withdrawal journal followed by normal creation, with a possible absence until
creation succeeds; stop all copies on managed-file drift or uncertain journals;
refuse transfer into an already published edition. Preserve recovery and show
these limits before confirmation. This avoids a new transaction coordinator.
Reconcile before every catalogue write after restart. A pending or blocked
decision gates ordinary publication too. A verified return uses the cycle's
explicit restoration role and the exact retained revision/source; returning to
this asset's own conforming published copy is a no-change exception. Evict only
unreferenced temporary previews when their quota is full. Check deterministic
creation prerequisites before withdrawal.
