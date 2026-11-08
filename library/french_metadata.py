"""Neutral Bedetheque reference parsing, adapted from pinned MIT cbzforge.

Copyright (c) 2026 Benoît Chevalier. See vendor/cbzforge/LICENSE and README.md.
No provider response establishes an operator decision or an exact edition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from library.catalog import LibraryError

BASE = 'https://www.bedetheque.com'
PARSER_VERSION = 'cbzforge-67823315-booksys-adapter-1'


@dataclass(frozen=True)
class SeriesReference:
    reference_id: str
    label: str
    url: str


@dataclass(frozen=True)
class ReferenceCandidate:
    reference_id: str
    url: str
    series: str | None
    title: str | None
    number: str | None
    fields: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def to_match_candidate(self) -> dict[str, Any]:
        values = {'title': self.title, 'series': self.series, 'number': self.number, **self.fields}
        return {'source': 'bedetheque', 'upstream': 'bedetheque',
                'work_ref': None, 'edition_ref': self.reference_id, 'url': self.url,
                **values, 'author': ', '.join(self.fields.get('writers', [])) or None,
                'field_provenance': {key: self.provenance for key, value in values.items()
                                     if value is not None and value != '' and value != []},
                'provenance': self.provenance}


def reference_url(value: str) -> str:
    url = urljoin(BASE, value)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LibraryError('Invalid Bedetheque reference port') from exc
    if (parsed.scheme != 'https' or parsed.hostname != 'www.bedetheque.com'
            or parsed.username or parsed.password or port or parsed.fragment):
        raise LibraryError('Unqualified Bedetheque reference URL')
    return url


def page_id(url: str, kind: str) -> str | None:
    path = urlsplit(reference_url(url)).path
    patterns = (r'/serie/index/s/(\d+)(?:/|$)', r'/serie-(\d+)-') if kind == 'series' else (
        r'/album/index/i/(\d+)(?:/|$)', r'/BD-[^/]*-(\d+)\.html$')
    return next((match.group(1) for pattern in patterns if (match := re.search(pattern, path))), None)


def text(node: Any) -> str | None:
    return ' '.join(node.get_text(' ', strip=True).split()) or None if node else None


def parse_series_search(body: str) -> list[SeriesReference]:
    """Preserve all public HTML results. Navigation and off-origin links are ignored."""
    soup = BeautifulSoup(body, 'html.parser')
    results: dict[str, SeriesReference] = {}
    for link in soup.select('a[href]'):
        if link.find_parent(['nav', 'header', 'footer']):
            continue
        try:
            url = reference_url(str(link['href']))
            reference = page_id(url, 'series')
        except LibraryError:
            continue
        label = text(link.select_one('.bdt-liste-libelle b')) or text(link)
        if reference and label:
            results[reference] = SeriesReference(reference, label, url)
    return list(results.values())


def provenance(url: str, retrieved_at: str) -> dict[str, str]:
    return {'source': 'bedetheque', 'upstream': 'bedetheque', 'url': url,
            'retrieved_at': retrieved_at, 'parser': PARSER_VERSION,
            'validation': 'source_assertion'}


def parse_series_albums(body: str, series: str, retrieved_at: str) -> list[ReferenceCandidate]:
    """Port of cbzforge parseSeriesAlbums, without first-result selection."""
    soup = BeautifulSoup(body, 'html.parser')
    results: dict[str, ReferenceCandidate] = {}
    for item in soup.select('ul.bdt-box-albums-liste > li'):
        link = item.select_one('a[href]')
        if link is None:
            continue
        try:
            url = reference_url(str(link['href']))
            reference = page_id(url, 'album')
        except LibraryError:
            continue
        title = text(link.select_one('.bdt-box-albums-titre')) or text(link)
        if not reference or not title or 'creation' in url:
            continue
        volume = re.search(r'Tome-(\d+)-', url)
        number = volume.group(1) if volume else None
        results[reference] = ReferenceCandidate(reference, url, series, title, number,
            {'integral': None}, provenance(url, retrieved_at))
    return list(results.values())


def parse_album_page(body: str, url: str, retrieved_at: str) -> ReferenceCandidate:
    """Port of cbzforge parseAlbumPage; unknown values remain unknown."""
    url = reference_url(url)
    reference = page_id(url, 'album')
    if not reference:
        raise LibraryError('Missing album reference ID')
    soup = BeautifulSoup(body, 'html.parser')
    def value(selector: str, attribute: str | None = None) -> str | None:
        node = soup.select_one(selector)
        if node is None:
            return None
        return str(node.get(attribute, '')).strip() or None if attribute else text(node)
    series = value('h1 a')
    full_name = value('meta[itemprop="name"]', 'content')
    title = full_name
    if full_name and series:
        title = full_name[len(series) + 3:].strip() if full_name.startswith(series + ' - ') else (
            None if full_name == series else full_name)
    if full_name and series and ' - ' in full_name:
        prefix, suffix = full_name.split(' - ', 1)
        if normalized_work_label(prefix) == normalized_work_label(series):
            title = suffix.strip() or None
    headline = value('meta[itemprop="alternativeheadline"]', 'content')
    volume = re.search(r'\b(?:Tome|Volume)\s+(\d+)\b', headline or '', re.I)
    number = volume.group(1) if volume else (headline.strip() if headline and re.fullmatch(r'\d+', headline.strip()) else None)
    contributors = []
    roles: dict[str, list[str]] = {'writers': [], 'artists': [], 'inkers': [], 'colorists': []}
    for node in soup.select('div.liste-auteurs > span'):
        name = text(node.select_one('a')); role = text(node.select_one('small'))
        if not name or not role or name.startswith('<'):
            continue
        if ',' in name:
            surname, given = name.split(',', 1); name = given.strip() + ' ' + surname.strip()
        contributors.append({'name': name, 'role': role})
        for token, destination in [('Scénario', 'writers'), ('Dessin', 'artists'), ('Encre', 'inkers'), ('Couleur', 'colorists')]:
            if token in role:
                roles[destination].append(name)
    summary = value('span[itemprop="description"]')
    if summary:
        summary = summary.replace('Lire la suite', '').strip()
    fields: dict[str, Any] = {**roles, 'contributors': contributors,
        'publisher': value('span[itemprop="publisher"]'),
        'date': value('meta[itemprop="datePublished"]', 'content'),
        'isbn': value('input#EAN', 'value'), 'summary': summary,
        'cover_url': value('img[itemprop="image"]', 'src'),
        'language': None, 'genres': [], 'adult': None, 'recommended_age': None,
        'family': None, 'integral': None}
    series_link = soup.select_one('h1 a[href]')
    try:
        fields['series_reference'] = page_id(str(series_link['href']), 'series') if series_link else None
    except LibraryError:
        fields['series_reference'] = None
    fields['language'] = value('meta[itemprop="inLanguage"]', 'content')
    genre = value('meta[itemprop="genre"]', 'content')
    if genre:
        labels = [part.strip() for part in genre.split(' - ') if part.strip()]
        families = {'Franco-Belge': 'BD', 'Manga': 'Manga', 'Comics': 'Comics'}
        fields['family'] = families.get(labels[0]) if labels else None
        fields['genres'] = [part.strip() for label in (labels[1:] if fields['family'] else labels)
                            for part in label.split(',') if part.strip()]
        words = ''.join(char for char in unicodedata.normalize('NFKD', genre.casefold())
                        if not unicodedata.combining(char))
        if re.search(r'\b(?:erotique|pornographique|adultes?|hentai)\b', words):
            fields['adult'] = True
    badges = [text(node) for node in soup.select('.bdt-ah-main .bdt-badge, .bdt-ah-sub .bdt-numa')]
    if ((headline and re.search(r'\b(?:int[ée]grale|INT)\b', headline, re.I)) or
            any(label and re.fullmatch(r'Int[ée]grale|INT\d*', label, re.I) for label in badges)):
        fields['integral'] = True
    # Additional field mapping can only use explicit labelled assertions.
    # No family/language/age is inferred from the website, name or cover.
    return ReferenceCandidate(reference, url, series, title, number, fields, provenance(url, retrieved_at))


@dataclass(frozen=True)
class DiscoveryResult:
    status: str
    candidates: list[ReferenceCandidate] = field(default_factory=list)
    reason: str = ''
    truncated: bool = False


def normalized_work_label(value: str) -> str:
    """cbzforge article inversion represented as a neutral comparison key."""
    value = value.casefold().replace('’', "'")
    inverted = re.match(r"^(.*?)\s+\((le|la|les|un|une|des|l')\)$", value)
    if inverted:
        article = inverted.group(2)
        value = article + ('' if article.endswith("'") else ' ') + inverted.group(1)
    value = ''.join(char for char in unicodedata.normalize('NFKD', value)
                    if not unicodedata.combining(char))
    value = re.sub(r"^(?:(?:le|la|les|un|une|des)\s+|l')", '', value)
    return ' '.join(re.findall(r'[^\W_]+', value))


def select_series(references: list[SeriesReference], query: str) -> list[SeriesReference]:
    exact = [row for row in references if normalized_work_label(row.label) == normalized_work_label(query)]
    if exact:
        return exact
    tokens = set(normalized_work_label(query).split())
    return [row for row in references if tokens and tokens <= set(normalized_work_label(row.label).split())]


def discover(source: Any, query: str, *, max_series: int = 3) -> DiscoveryResult:
    """Public index adapter. Every bound is visible; truncation prevents auto-choice.

    Source has get(url)->ProviderPage. It owns robots, cache and request budget.
    No arbitrary URLs, AJAX, image fetches, first result or cover ranking is used.
    """
    if not 1 <= max_series <= 3:
        raise LibraryError('Discovery series bound must be between one and three')
    key = normalized_work_label(query)
    letter = key[:1].upper()
    letter = letter if re.fullmatch('[A-Z]', letter) else '0'
    page = source.get(BASE + '/bandes_dessinees_' + letter + '.html')
    if page.status != 'ok':
        return DiscoveryResult(page.status, reason=page.reason)
    references = parse_series_search(page.body)
    if not references:
        return DiscoveryResult('unexpected_response', reason='Public series index has no reference rows')
    plausible = select_series(references, query)
    if not plausible:
        return DiscoveryResult('no_candidate', reason='No plausible series label in the public letter index')
    if len(plausible) > max_series:
        return DiscoveryResult('ambiguous', reason='Plausible series exceed the complete discovery bound', truncated=True)
    candidates: dict[str, ReferenceCandidate] = {}
    for reference in plausible:
        series = source.get(reference.url)
        if series.status != 'ok':
            return DiscoveryResult(series.status, list(candidates.values()), reason=series.reason, truncated=True)
        rows = parse_series_albums(series.body, reference.label, series.retrieved_at)
        if not rows:
            return DiscoveryResult('unexpected_response', list(candidates.values()),
                                   reason='Recognized series has no complete album list', truncated=True)
        for row in rows:
            candidates[row.reference_id] = row
    return DiscoveryResult('ok', list(candidates.values()), reason='All retained plausible series album rows')


def parse_album_editions(body: str, url: str, retrieved_at: str) -> list[ReferenceCandidate]:
    """Keep all visible edition assertions without assigning an edition to a file.

    Canonical page metadata is parsed by the pinned selector port. Compact edition
    cards carry their own title, credits, publisher/date and ISBN. These may differ.
    """
    canonical = parse_album_page(body, url, retrieved_at)
    result = {canonical.reference_id: canonical}
    soup = BeautifulSoup(body, 'html.parser')
    for card in soup.select('dl.bdt-sheet--compact'):
        values: dict[str, list[Any]] = {}
        previous = ''
        for node in card.find_all(['dt', 'dd'], recursive=False):
            if node.name == 'dt':
                previous = text(node) or previous
            else:
                values.setdefault(previous, []).append(node)
        def label(*names: str) -> str | None:
            for name in names:
                nodes = values.get(name, [])
                if nodes:
                    return text(nodes[0])
            return None
        reference = label('Identifiant')
        if not reference or not re.fullmatch(r'\d+', reference):
            continue
        if reference == canonical.reference_id:
            continue
        fields = {**canonical.fields}
        fields.update(publisher=label('Éditeur', 'Editeur'), date=label('Dépôt légal', 'Dépot légal'),
                      isbn=label('EAN/ISBN', 'ISBN'), edition_record=True)
        contributors=[]
        for role, destination in [('Scénario','writers'),('Dessin','artists'),('Couleurs','colorists'),('Encre','inkers')]:
            people=[]
            for node in values.get(role, []):
                for link in node.select('a'):
                    name=text(link)
                    if name and not name.startswith('<'):
                        if ',' in name:
                            surname,given=name.split(',',1); name=given.strip()+' '+surname.strip()
                        people.append(name);contributors.append({'name':name,'role':role})
            fields[destination]=list(dict.fromkeys(people))
        fields['contributors']=contributors
        # Work-wide text remains an assertion shared by the page, not proof that
        # the edition-specific contributors/ISBN match the file.
        reference_url_value=BASE+'/album/index/i/'+reference
        result[reference]=ReferenceCandidate(reference,reference_url_value,canonical.series,
            label('Titre') or canonical.title,canonical.number,fields,
            {**canonical.provenance,'edition_record_id':reference})
    return list(result.values())
