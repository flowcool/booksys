"""Exact ISBN resolution from BnF SRU UNIMARC. Descriptive data: Licence Ouverte."""
from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from library.catalog import LibraryError

SRU = '{http://www.loc.gov/zing/srw/}'
MARC = '{info:lc/xmlns/marcxchange-v2}'


def parse_bnf(body: str, code: str, url: str, retrieved_at: str) -> dict[str, Any]:
    if '<!DOCTYPE' in body.upper() or '<!ENTITY' in body.upper():
        raise LibraryError('Unsafe BnF XML')
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise LibraryError('Malformed BnF XML') from exc
    records = root.findall('.//' + MARC + 'record')
    if root.findtext(SRU + 'numberOfRecords') != '1' or len(records) != 1:
        raise LibraryError('BnF edition absent or ambiguous')
    record = records[0]
    if record.get('format') != 'UNIMARC' or record.get('type') != 'Bibliographic':
        raise LibraryError('Unsupported BnF record')
    def values(tag: str, subfield: str) -> list[str]:
        return [str(node.text).strip() for field in record.findall(MARC + 'datafield')
                if field.get('tag') == tag for node in field.findall(MARC + 'subfield')
                if node.get('code') == subfield and node.text and node.text.strip()]
    def one(tag: str, subfield: str) -> str:
        found = values(tag, subfield)
        return found[0] if len(found) == 1 else ''
    # fuzzyISBN locates ISBN10/13 records. Exact EAN in the returned record still
    # has to match; a fuzzy search response alone is never an identity proof.
    if code not in [re.sub(r'[-\s]', '', v) for v in values('073', 'a') + values('010', 'a')]:
        raise LibraryError('BnF response does not contain the exact ISBN13/EAN')
    reference = record.get('id', '')
    if not re.fullmatch(r'ark:/12148/cb[0-9a-z]+', reference):
        raise LibraryError('Missing BnF edition ARK')
    medium = one('200', 'b')
    if medium != 'Texte imprimé':
        raise LibraryError('BnF medium is not a qualified print edition')
    surname, given = one('700', 'a'), one('700', 'b')
    number = one('461', 'v')
    series_ref = one('461', '0')
    return {'source': 'bnf', 'upstream': 'bnf', 'url': url, 'retrieved_at': retrieved_at,
            'isbn': code, 'title': one('200', 'a'), 'series': one('225', 'a'),
            'author': ' '.join(x for x in (given, surname) if x),
            'author_forms': [' '.join(x for x in (surname, given) if x)],
            'number': number, 'publisher': one('210', 'c'),
            'language': {'fre': 'fr', 'eng': 'en', 'jpn': 'ja'}.get(one('101', 'a'), ''),
            'date': one('210', 'd'), 'revision': one('205', 'a'), 'medium': 'print',
            'work_ref': f'series:{series_ref}:volume:{number}' if series_ref and number else '',
            'edition_ref': reference, 'attribution': 'Bibliothèque nationale de France, Licence Ouverte'}
