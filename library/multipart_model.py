"""Select exact tracked RAR volume sets without scanning reception."""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import urlsplit
from library.primitives import LibraryError

PART = re.compile(r"(.+)\.part([0-9]{1,3})\.rar", re.I)

def is_multipart(name: str) -> bool:
    return bool(re.search(r'\.part[0-9]+\.rar$', name, re.I) or re.search(r"(?:\.r[0-9]{2,3}|\.z[0-9]{2,3}|\.(?:zip|7z)\.[0-9]+)$", name, re.I))

def select_parts(links: list[dict[str, Any]], item: dict[str, Any]) -> list[dict[str, Any]]:
    match = PART.fullmatch(item['name'])
    if not match:
        raise LibraryError('Unsupported multipart type; only numbered RAR volumes are accepted')
    host = urlsplit(item.get('url', '')).hostname
    if any(ord(char) < 32 or char in ',/\\' for char in item['name']):
        raise LibraryError('Unsupported multipart unsafe filename')
    if not host:
        raise LibraryError('Unsupported multipart without tracked host provenance')
    members = []
    for candidate in links:
        other = PART.fullmatch(candidate['name'])
        if (other and other[1] == match[1] and candidate.get('input_folder') == item.get('input_folder')
                and urlsplit(candidate.get('url', '')).hostname == host):
            members.append((int(other[2]), candidate))
    members.sort(key=lambda pair: pair[0])
    if not 2 <= len(members) <= 64 or [n for n,_ in members] != list(range(1,len(members)+1)):
        raise LibraryError('Missing part or duplicate multipart number')
    numbers = [PART.fullmatch(candidate['name'])[2] for _,candidate in members]  # type: ignore[index]
    if len({len(number) for number in numbers}) != 1 and any(number != str(int(number)) for number in numbers):
        raise LibraryError('Unsupported multipart naming mismatch')
    if any(type(candidate.get('status')) is not int or candidate['status'] != 0 for _,candidate in members):
        raise LibraryError('Awaiting part: every tracked volume must be finished')
    return [candidate for _,candidate in members]
