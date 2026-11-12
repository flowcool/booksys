"""Pure interpretation of retained printed evidence."""
from __future__ import annotations
import re
import unicodedata
from typing import Any
from library.bibliographic import isbn13, normalized
from library.primitives import now

def printed_isbns(printed: dict[str, Any]) -> tuple[set[str], bool]:
    codes: set[str] = set()
    invalid = False
    for page in printed.get('pages', []):
        for line in page.get('text', '').splitlines():
            labelled = list(re.finditer(r'\bISBN(?:[- ]?13)?\s*[:：]?\s*([0-9][0-9\s\-\"\']+)', line, re.I))
            values = [match.group(1) for match in labelled] if labelled else [line.strip()]
            for raw in values:
                if not labelled and not re.fullmatch(r'[0-9\s\-\"\'.]+', raw):
                    continue
                digits = re.sub(r'[^0-9]', '', raw)
                if not labelled and len(digits) != 13:
                    continue
                code = isbn13(digits)
                if code:
                    codes.add(code)
                elif labelled or len(digits) == 13:
                    invalid = True
    return codes, invalid


def words(value: str) -> str:
    text = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', text))
