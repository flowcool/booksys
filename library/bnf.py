"""Exact ISBN resolution from BnF SRU UNIMARC. Descriptive data: Licence Ouverte."""
from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from library.primitives import LibraryError

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
        raise LibraryError('BnF edition not found' if root.findtext(SRU + 'numberOfRecords') == '0' else 'BnF edition ambiguous')
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
    contributors = []
    writers = []
    author_forms = []
    responsibility = values('200', 'f')
    for field in record.findall(MARC + 'datafield'):
        tag = field.get('tag', '')
        if tag not in ('700', '701', '702', '710', '711', '712'):
            continue
        subs: dict[str, list[str]] = {}
        for node in field.findall(MARC + 'subfield'):
            if node.text:
                subs.setdefault(str(node.get('code')), []).append(node.text.strip())
        surname = ' '.join(subs.get('a', []))
        given = ' '.join(subs.get('b', [])) if tag.startswith('70') else ''
        name = ' '.join(value for value in (given, surname) if value)
        roles = subs.get('c', []) + subs.get('4', [])
        contributors.append({'name': name, 'roles': roles, 'tag': tag,
                             'authority': subs.get('3', [])})
        explicit_writer = any(re.search(r'sc[ée]nari|writer', role, re.I) for role in roles)
        responsibility_writer = any(re.search(r'sc[ée]nari', value, re.I) and surname.casefold() in value.casefold()
                                    for value in responsibility) if surname else False
        # Main author is qualified unless explicitly an illustrator/translator.
        main_writer = tag == '700' and not any(role in ('040', '440', '730') or re.search(r'illustr|traduct|dessin', role, re.I) for role in roles)
        if name and (explicit_writer or responsibility_writer or main_writer):
            writers.append(name)
            author_forms.append(' '.join(value for value in (surname, given) if value))
    author = ', '.join(dict.fromkeys(writers))
    number = one('461', 'v')
    series_ref = one('461', '0')
    return {'source': 'bnf', 'upstream': 'bnf', 'url': url, 'retrieved_at': retrieved_at,
            'isbn': code, 'title': one('200', 'a'), 'series': one('225', 'a'),
            'author': author, 'author_forms': author_forms, 'contributors': contributors,
            'responsibility': {'primary': responsibility, 'secondary': values('200', 'g')},
            'number': number, 'unnumbered': not number and not series_ref,
            'publisher': one('214', 'c') or one('210', 'c'),
            'language': {'fre': 'fr', 'eng': 'en', 'jpn': 'ja'}.get(one('101', 'a'), ''),
            'date': one('214', 'd') or one('210', 'd'), 'revision': one('205', 'a'), 'medium': 'print',
            'work_ref': (f'series:{series_ref}:volume:{number}' if series_ref and number else
                         'edition-scoped:' + reference if not number and not series_ref else ''),
            'edition_ref': reference, 'attribution': 'Bibliothèque nationale de France, Licence Ouverte'}
