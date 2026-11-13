"""Accepted application/entrypoint dependency boundary, including local imports."""
import ast
from pathlib import Path
import unittest


class BoundaryTests(unittest.TestCase):
    def test_application_modules_do_not_import_entrypoints(self):
        root = Path(__file__).resolve().parents[1]
        paths = [*root.glob('booksys_core/*.py'), *root.glob('library/*.py'),
                 *(root / name for name in ('fiches.py', 'mirrors.py', 'mirror_routing.py', 'retirement_history.py'))]
        edges = []
        for path in paths:
            if path.name == 'cli.py':
                continue  # The library CLI is itself an entry point.
            for node in ast.walk(ast.parse(path.read_text())):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) else
                           [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    if module and (module in ('booksys', 'pipeline', 'library.cli', '__main__') or module.startswith('ops.')):
                        edges.append(f'{path.relative_to(root)}:{node.lineno} -> {module}')
        self.assertEqual(edges, [])
