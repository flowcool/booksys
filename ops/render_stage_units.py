"""Render candidate user units to a new directory. Never install or activate."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.stage_supervisor import units


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('code', 'python', 'root', 'collector', 'reception', 'credentials', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--reader-image', default='')
    args = parser.parse_args()
    rendered = units(code=args.code, python=args.python, root=args.root, collector=args.collector,
                     reception=args.reception, credentials=args.credentials, reader_image=args.reader_image)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in rendered.items():
        with (args.output/name).open('x') as stream:
            stream.write(content)


if __name__ == '__main__':
    main()
