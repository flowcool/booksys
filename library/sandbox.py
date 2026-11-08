"""Build an empty-root native worker with explicit runtime/input grants."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import sysconfig

from library.catalog import LibraryError

PYTHON = '/runtime/python/bin/python'


def native_prefix(source: Path, *, output: Path | None = None, ocr: bool = False) -> list[str]:
    """Expose trusted code/libraries and one input; never bind the host root."""
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
               '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--clearenv',
               '--dir', '/runtime/python/bin', '--dir', '/runtime/tools', '--dir', '/app']
    for host in ('/usr/lib', '/usr/lib64'):
        if Path(host).is_dir():
            command += ['--ro-bind', host, host]
    command += ['--symlink', 'usr/lib', '/lib']
    if Path('/usr/lib64').is_dir():
        command += ['--symlink', 'usr/lib64', '/lib64']
    if Path('/etc/ld.so.cache').is_file():
        command += ['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache']
    base = Path(sys.base_prefix).resolve()
    library = base / 'lib'
    executable = Path(sys.executable).resolve()
    if not library.is_dir() or not executable.is_file():
        raise LibraryError('Unsupported native Python runtime layout')
    command += ['--ro-bind', str(library), '/runtime/python/lib',
                '--ro-bind', str(executable), PYTHON,
                '--ro-bind', str(Path(__file__).resolve().parent), '/app/library']
    packages = []
    for kind in ('purelib', 'platlib'):
        host_path = str(Path(sysconfig.get_path(kind)).resolve())
        if host_path not in packages:
            packages.append(host_path)
            command += ['--ro-bind', host_path, '/runtime/packages' + str(len(packages))]
    pythonpath = ':'.join(['/app', *['/runtime/packages' + str(i + 1) for i in range(len(packages))]])
    command += ['--setenv', 'PYTHONHOME', '/runtime/python', '--setenv', 'PYTHONPATH', pythonpath,
                '--chdir', '/app', '--ro-bind', str(source.absolute()), '/tmp/input']
    if output is not None:
        command += ['--bind', str(output.absolute()), '/tmp/output']
    def tool(value: str | None, target: str) -> None:
        if value:
            path = Path(value).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                command.extend(['--ro-bind', str(path), target])
    if source.suffix.lower() in ('.cbr', '.rar'):
        tool(os.environ.get('BOOKSYS_UNRAR') or shutil.which('unrar'), '/runtime/tools/unrar')
        command += ['--setenv', 'BOOKSYS_UNRAR', '/runtime/tools/unrar']
    if source.suffix.lower() == '.pdf' and (ocr or output is not None):
        tool(os.environ.get('BOOKSYS_PDFTOPPM') or shutil.which('pdftoppm'), '/runtime/tools/pdftoppm')
        command += ['--setenv', 'BOOKSYS_PDFTOPPM', '/runtime/tools/pdftoppm']
        for directory in ('/etc/fonts', '/usr/share/fonts', '/usr/share/fontconfig'):
            if Path(directory).is_dir():
                command += ['--ro-bind', directory, directory]
    if ocr:
        tool(os.environ.get('BOOKSYS_TESSERACT') or shutil.which('tesseract'), '/runtime/tools/tesseract')
        command += ['--setenv', 'BOOKSYS_TESSERACT', '/runtime/tools/tesseract']
        data = Path(os.environ.get('BOOKSYS_TESSDATA', '/usr/share/tesseract-ocr/5/tessdata')).resolve()
        if data.is_dir():
            command += ['--ro-bind', str(data), '/runtime/tessdata']
        command += ['--setenv', 'TESSDATA_PREFIX', '/runtime/tessdata']
        if os.environ.get('BOOKSYS_OCR_LIBDIR'):
            libs = Path(os.environ['BOOKSYS_OCR_LIBDIR']).resolve()
            if libs.is_dir():
                command += ['--ro-bind', str(libs), '/runtime/ocr-libs',
                            '--setenv', 'LD_LIBRARY_PATH', '/runtime/ocr-libs:/runtime/python/lib']
    return command
