"""ComicInfo 2.0 export from the local catalogue; upstream MIT XSD validation."""
from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from lxml import etree  # type: ignore[import-untyped]

from library.primitives import LibraryError


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


WORK_XML_FIELDS = {'title': 'Title', 'series': 'Series', 'number': 'Number',
    'summary': 'Summary', 'notes': 'Notes', 'author': 'Writer', 'publisher': 'Publisher',
    'genres': 'Genre', 'url': 'Web', 'language': 'LanguageISO', 'age_rating': 'AgeRating'}


def work_comicinfo(asset: dict[str, Any], work: dict[str, Any]) -> bytes:
    """Export common work fields while retaining supported source annotations.

    Raw source XML stays in the asset. Unsupported source elements are omitted
    from the strict 2.0 export. ISBN, adult and numeric recommended_age remain
    central metadata: none implies a schema AgeRating. Explicit overrides win.
    field_conflicts removes stale source values unless an override resolves it.
    """
    schema_root = ElementTree.parse(Path(__file__).parent / 'schemas/ComicInfo-2.0.xsd').getroot()
    ns = {'xs': 'http://www.w3.org/2001/XMLSchema'}
    sequence = schema_root.find("xs:complexType[@name='ComicInfo']/xs:sequence", ns)
    if sequence is None:
        raise LibraryError('Pinned ComicInfo schema has no field sequence')
    order = [element.attrib['name'] for element in sequence if 'name' in element.attrib]
    elements: dict[str, Any] = {}
    encoded = asset.get('source_comicinfo_base64')
    if encoded:
        try:
            source = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise LibraryError('Invalid source ComicInfo encoding') from exc
        if len(source) > 1024**2 or b'<!DOCTYPE' in source.upper() or b'<!ENTITY' in source.upper():
            raise LibraryError('Unsafe or oversized ComicInfo XML')
        try:
            original = ElementTree.fromstring(source)
        except ElementTree.ParseError as exc:
            raise LibraryError('Invalid source ComicInfo XML') from exc
        if original.tag != 'ComicInfo':
            raise LibraryError('Invalid source ComicInfo root')
        for element in original:
            if element.tag in order:
                elements[element.tag] = deepcopy(element)
    overrides = asset.get('overrides', {})
    for field in work.get('field_conflicts', {}):
        if field not in overrides and field == 'date':
            for tag in ('Year', 'Month', 'Day'):
                elements.pop(tag, None)
        if field not in overrides and field == 'contributors':
            for tag in ('Writer', 'Penciller', 'Inker', 'Colorist', 'Letterer', 'CoverArtist', 'Editor'):
                elements.pop(tag, None)
        if field not in overrides and field in WORK_XML_FIELDS:
            elements.pop(WORK_XML_FIELDS[field], None)
    fields = {**work, **overrides}
    for field in work.get('field_conflicts', {}):
        if field not in overrides:
            fields.pop(field, None)
    roles = {'writer': 'Writer', 'penciller': 'Penciller', 'inker': 'Inker', 'colorist': 'Colorist',
             'letterer': 'Letterer', 'cover_artist': 'CoverArtist', 'editor': 'Editor'}
    if 'date' in fields:
        for tag in ('Year', 'Month', 'Day'):
            elements.pop(tag, None)
        date = str(fields['date']) if fields['date'] is not None else ''
        if re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', date):
            for tag, value in zip(('Year', 'Month', 'Day'), date.split('-')):
                element = ElementTree.Element(tag)
                element.text = str(int(value))
                elements[tag] = element
    contributors = fields.get('contributors')
    if isinstance(contributors, list):
        mapped: dict[str, list[str]] = {}
        for person in contributors:
            if not isinstance(person, dict) or not person.get('name'):
                continue
            for token, role in (('scénario', 'writer'), ('dessin', 'penciller'),
                                ('encre', 'inker'), ('couleur', 'colorist'),
                                ('lettrage', 'letterer')):
                if token in str(person.get('role', '')).casefold():
                    mapped.setdefault(role, []).append(str(person['name']))
        contributors = mapped
    if isinstance(contributors, dict):
        for role, tag in roles.items():
            if contributors.get(role):
                value = contributors[role]
                element = ElementTree.Element(tag)
                element.text = ', '.join(map(str, value)) if isinstance(value, list) else str(value)
                elements[tag] = element
    for field, tag in WORK_XML_FIELDS.items():
        if field not in fields:
            continue
        value = fields[field]
        if field == 'genres' and isinstance(value, list):
            value = ', '.join(str(item) for item in value)
        elements.pop(tag, None)
        if value is not None and value != '':
            element = ElementTree.Element(tag)
            element.text = str(value)
            elements[tag] = element
    if asset.get('pages') is not None:
        element = ElementTree.Element('PageCount')
        element.text = str(asset['pages'])
        elements['PageCount'] = element
    root = ElementTree.Element('ComicInfo')
    for tag in order:
        if tag in elements:
            root.append(elements[tag])
    data = ElementTree.tostring(root, encoding='utf-8', xml_declaration=True)
    validate_xml(data)
    return bytes(data)
