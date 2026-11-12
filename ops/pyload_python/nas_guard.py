"""Refuse pyLoad startup on any substitute for the qualified downloads bind."""
import hashlib
import os
from pathlib import Path


def verify():
    expected = os.environ['BOOKSYS_NFS_SOURCE']
    rows = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    for target, root, readonly in (('/downloads', '/downloads', False), ('/nas-identity', '/', True)):
        matches = [row for row in rows if row[4] == target]
        if len(matches) != 1:
            raise SystemExit('Qualified NAS bind is absent')
        row = matches[0]
        separator = row.index('-')
        source = expected + (root if root != '/' else '')
        if (row[separator+1:separator+3] != ['nfs', source] or row[3] != '/'
                or ('ro' in row[5].split(',')) != readonly or 'hard' not in row[separator+3].split(',')):
            raise SystemExit('Qualified NAS identity differs')
    sentinel = Path('/nas-identity/.nas-identity')
    if sentinel.is_symlink() or sentinel.stat().st_size > 4096:
        raise SystemExit('Invalid NAS sentinel')
    if hashlib.sha256(sentinel.read_bytes()).hexdigest() != os.environ['BOOKSYS_NFS_SENTINEL_SHA256']:
        raise SystemExit('NAS sentinel differs')


def main():
    verify()
    os.execv('/init', ['/init'])


if __name__ == '__main__':
    main()
