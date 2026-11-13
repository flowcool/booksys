"""Pure allowlisted presentation fields; no storage or service dependency."""
from __future__ import annotations
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlsplit

def bounded(value: Any, length: int = 240) -> str:
    return str(value or '')[:length]


def source_url(value: Any) -> str:
    text = bounded(value, 1000)
    try:
        parsed = urlsplit(text)
        if (parsed.scheme == 'https' and parsed.hostname in ('catalogue.bnf.fr', 'openlibrary.org',
                'www.bdtheque.com', 'api.mangadex.org', 'mangadex.org')
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            return text
    except ValueError:
        pass
    return ''


def fields(value: dict[str, Any], names: tuple[str, ...], limit: int = 500) -> dict[str, str]:
    return {name: bounded(value[name], limit) for name in names if value.get(name) is not None}


def description_text(value: Any) -> str:
    class Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.hidden = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in ('script', 'style'):
                self.hidden += 1
            elif not self.hidden and tag in ('p', 'div', 'br', 'li'):
                self.parts.append('\n\n')

        def handle_endtag(self, tag: str) -> None:
            if tag in ('script', 'style'):
                self.hidden = max(0, self.hidden - 1)
            elif not self.hidden and tag in ('p', 'div', 'li'):
                self.parts.append('\n\n')

        def handle_data(self, data: str) -> None:
            if not self.hidden:
                self.parts.append(data)
    parser = Text()
    parser.feed(bounded(value, 12000))
    return '\n\n'.join(' '.join(p.split()) for p in ''.join(parser.parts).split('\n\n') if p.strip())[:4000]


def operator_reason(value: Any) -> str:
    reason = bounded(value)
    messages = {
        'Transfer is not confirmed finished': 'Téléchargement non terminé : le fichier sera examiné après sa fin.',
        'Skipped mirror link': 'Miroir ignoré par pyLoad. Ce lien ne fournit aucun fichier validé.',
        'Offline mirror link': 'Miroir hors ligne. Ce lien ne fournit aucun fichier validé.',
        'Failed mirror link': 'Miroir en échec. Consulter pyLoad.',
        'Downloader reports an error; inspect pyLoad': 'Le téléchargement signale une erreur. Consulter pyLoad.',
        'Printed ISBN is missing, invalid or contradictory in the bounded window': 'ISBN absent, invalide ou contradictoire dans les pages examinées.',
        'Non-ISBN identity requires two independent upstream sources': 'Sans ISBN, deux sources indépendantes doivent confirmer cette édition.',
        'Multipart representation': 'Fichier en plusieurs parties : assemblage non pris en charge.',
        'Multiple image groups: possible multi-work archive': 'Plusieurs groupes d’images : cette archive peut contenir plusieurs ouvrages.',
        'Unsupported or ambiguous suffix': 'Extension de fichier non prise en charge ou ambiguë.',
        'LibraryError: Nested container or unauthorized archive content': 'Archive imbriquée ou contenu non autorisé.',
        'LibraryError: Active PDF content or attachment': 'PDF avec contenu actif ou pièce jointe : traitement refusé.',
        'Metadata not attempted': 'Métadonnées à examiner lors d’un prochain cycle.',
    }
    return messages.get(reason, reason)


def terminal_transfer(status: Any) -> tuple[str, str] | None:
    """Pinned pyLoad terminal states affect presentation, never file admission."""
    if type(status) is not int:
        return None
    return {4: ('stopped', 'Skipped mirror link'), 1: ('blocked', 'Offline mirror link'),
            8: ('blocked', 'Failed mirror link')}.get(status)




def public_value(value: Any, *, key: str = '') -> Any:
    """Filter every display scalar; approved bibliographic URLs retain provenance."""
    from booksys_core.diagnostics import safe_text
    if isinstance(value, dict):
        return {safe_text(str(name)): public_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [public_value(item, key=key) for item in value]
    if isinstance(value, str):
        if key == 'url':
            return source_url(value)
        if key in ('cover', 'fiche', 'bundle_parent') and re.fullmatch(r'/(?:cover/[0-9a-f]{64}\.png|fiche/[A-Za-z0-9_-]{1,64})', value):
            return value
        return safe_text(value, limit=12000 if key == 'summary' else 4000)
    if value is None or type(value) in (int, float, bool):
        return value
    return None
