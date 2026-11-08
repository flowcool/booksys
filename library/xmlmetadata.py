"""ComicInfo 2.0 export from the local catalogue; upstream MIT XSD validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from lxml import etree  # type: ignore[import-untyped]

from library.catalog import LibraryError


def validate_xml(data: bytes) -> None:
    if len(data) > 1024**2 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise LibraryError('Unsafe or oversized ComicInfo XML')
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(data, parser)
        schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / 'schemas/ComicInfo-2.0.xsd'), parser))
        schema.assertValid(root)
    except (etree.XMLSyntaxError, etree.DocumentInvalid) as exc:
        raise LibraryError('ComicInfo does not conform to the pinned 2.0 schema') from exc


def comicinfo(state: dict[str, Any], asset_id: str) -> bytes:
    asset = state['assets'][asset_id]
    identity = asset.get('identity')
    if not identity:
        raise LibraryError('Cannot export unknown work/edition')
    work, edition = state['works'][identity['work']], state['editions'][identity['edition']]
    fields = {**work, **asset.get('overrides', {})}
    values: dict[str, Any] = {'Title': fields['title'], 'Series': fields.get('series'),
        'Number': fields.get('number'), 'Summary': fields.get('summary'),
        'Notes': 'Booksys catalogue authority; work=' + identity['work'] + '; edition=' + identity['edition'],
        'Writer': fields.get('author'), 'Publisher': edition['publisher'],
        'Genre': ', '.join(fields.get('genres', [])) if isinstance(fields.get('genres'), list) else None,
        'PageCount': asset.get('pages'), 'LanguageISO': edition['language']}
    rating = fields.get('public')
    mapping = {'adult': 'Adults Only 18+', 'Adultes': 'Adults Only 18+',
               'all_ages': 'Everyone', 'safe': 'Everyone'}
    if rating in mapping:
        values['AgeRating'] = mapping[rating]
    root = ElementTree.Element('ComicInfo')
    for name in ('Title', 'Series', 'Number', 'Summary', 'Notes', 'Writer', 'Publisher', 'Genre',
                 'PageCount', 'LanguageISO', 'AgeRating'):
        value = values.get(name)
        if value is not None and value != '':
            ElementTree.SubElement(root, name).text = str(value)
    data = ElementTree.tostring(root, encoding='utf-8', xml_declaration=True)
    validate_xml(data)
    return bytes(data)
