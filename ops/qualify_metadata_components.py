"""Offline owned-fixture proof of selected cover code and existing ComicInfo 2.0 transport."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from xml.etree import ElementTree
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from library.catalog import LibraryError
from library.cover_candidates import METHOD, cover_hash, compare_covers
from library.readers import inspect_file, sha256
from library.transform import transform_archive
from library.xmlmetadata import comicinfo, validate_xml


def qualify(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    image = Image.new('RGB', (96, 128), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 8, 70, 35), fill='black')
    draw.ellipse((30, 55, 85, 118), fill='navy')
    buffer = io.BytesIO();image.save(buffer, format='PNG')
    image_bytes = buffer.getvalue()
    fingerprint = cover_hash(image)
    assert fingerprint is not None
    started = time.monotonic()
    for _ in range(10):
        assert cover_hash(image) == fingerprint
    milliseconds = (time.monotonic() - started) * 100
    ties = compare_covers(fingerprint, {'edition-a': fingerprint, 'edition-b': fingerprint})
    assert len(ties) == 2 and all(row['distance'] == 0 for row in ties)
    assert cover_hash(Image.new('RGB', image.size, 'white')) is None

    # The proposed field mapping remains a fixture, not an activated writer.
    # Adult is sourced centrally without manufacturing a numeric age.
    central = {'isbn': '9781234567897', 'adult': True, 'recommended_age': None,
               'adult_source': 'https://www.bedetheque.com/fixture',
               'genres': ['Aventure', 'Érotique'], 'manual_note': 'Operator annotation',
               'unmapped_source_fields': {'PrivateUnknown': 'Retained source evidence'}}
    source_xml = b'<ComicInfo><Title>Old title</Title><Summary>Prior summary</Summary><Notes>Operator annotation</Notes><Pages><Page Image="0" Bookmark="Chapter one" Type="FrontCover" /></Pages><PrivateUnknown>Retained source evidence</PrivateUnknown></ComicInfo>'
    source = output / 'source.cbz'
    with ZipFile(source, 'x') as archive:
        archive.writestr('001.png', image_bytes)
        archive.writestr('002.png', image_bytes)
        archive.writestr('ComicInfo.xml', source_xml)
    before = sha256(source)
    tree = ElementTree.fromstring(source_xml)
    assert tree.find('Title') is not None
    tree.find('Title').text = 'Qualified album'  # type: ignore[union-attr]
    ElementTree.SubElement(tree, 'Genre').text = ', '.join(central['genres'])  # type: ignore[arg-type]
    ElementTree.SubElement(tree, 'Web').text = str(central['adult_source'])
    unknown = tree.find('PrivateUnknown')
    assert unknown is not None and unknown.text == central['unmapped_source_fields']['PrivateUnknown']  # type: ignore[index]
    tree.remove(unknown)
    # ComicInfo 2.0 uses an ordered sequence, including after metadata merges.
    order = ['Title', 'Summary', 'Notes', 'Genre', 'Web', 'Pages']
    tree[:] = sorted(tree, key=lambda child: order.index(child.tag))
    mapped_xml = ElementTree.tostring(tree, encoding='utf-8', xml_declaration=True)
    validate_xml(mapped_xml)
    target = output / 'converted';target.mkdir()
    inspected = inspect_file(source, stable_seconds=0)
    assert inspected['status'] == 'validated', inspected
    result = transform_archive(source, target, mapped_xml, before)
    with ZipFile(target / 'book.cbz') as archive:
        readback = ElementTree.fromstring(archive.read('ComicInfo.xml'))
        pages = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in ('001.png', '002.png')}
        assert all(archive.read(name) == image_bytes for name in pages)
        assert readback.findtext('Title') == 'Qualified album'
        assert readback.findtext('Notes') == central['manual_note']
        assert readback.findtext('Summary') == 'Prior summary'
        assert readback.find('Pages/Page').get('Bookmark') == 'Chapter one'  # type: ignore[union-attr]
        assert readback.find('Pages/Page').get('Type') == 'FrontCover'  # type: ignore[union-attr]
        assert readback.find('PrivateUnknown') is None
        assert readback.find('AgeRating') is None
        assert readback.find('GTIN') is None
    assert sha256(source) == before
    assert result['page_manifest'] == inspected['page_manifest']
    # Fail-closed schema check documents why cix's added GTIN/Tags are not selected.
    rejected = []
    for field in ('GTIN', 'Tags', 'PrivateUnknown'):
        try:
            validate_xml(('<ComicInfo><' + field + '>value</' + field + '></ComicInfo>').encode())
        except LibraryError:
            rejected.append(field)
    assert rejected == ['GTIN', 'Tags', 'PrivateUnknown']
    # Record actual current-writer gaps instead of hiding them behind codec success.
    current_state = {'assets': {'asset': {'identity': {'work': 'work', 'edition': 'edition'},
        'pages': 2, 'overrides': {}}}, 'works': {'work': {'title': 'Album', 'public': 'adult'}},
        'editions': {'edition': {'publisher': 'Publisher', 'language': 'fr'}}}
    current = ElementTree.fromstring(comicinfo(current_state, 'asset'))
    current_gaps = {'adult_invents_numeric_age': current.findtext('AgeRating') == 'Adults Only 18+',
                    'source_annotations_not_merged': current.findtext('Notes') != central['manual_note']}
    proof = {'status': 'qualified_components_only', 'runtime_integration': False,
        'cover_method': METHOD, 'cover_hash_ms_mean': round(milliseconds, 3),
        'cover_ties_retained': ties, 'blank_cover_abstains': True,
        'source_sha256_before': before, 'source_sha256_after': sha256(source),
        'page_hashes_preserved': pages, 'page_manifest_preserved': True,
        'supported_fields_preserved': ['Title', 'Notes', 'Summary', 'Genre', 'Web', 'Pages/Page.Bookmark'],
        'unknown_age_omitted': True, 'unsupported_schema_fields_rejected': rejected,
        'isbn_retained_centrally': central['isbn'],
        'unmapped_source_fields_retained_centrally': central['unmapped_source_fields'], 'current_writer_gaps': current_gaps,
        'scope': 'synthetic owned CBZ only; no provider, Komga or runtime access'}
    (output / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New owned output directory; must not exist')
    args = parser.parse_args()
    print(json.dumps(qualify(args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
