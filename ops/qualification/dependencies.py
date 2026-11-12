"""Static application import graph, including imports inside functions."""
from __future__ import annotations
import ast
from pathlib import Path


def import_graph(root: Path) -> dict[str, set[str]]:
    paths = {str(path.relative_to(root).with_suffix('')).replace('/', '.'): path
             for folder in ('library', 'booksys_core') for path in (root / folder).glob('*.py')}
    graph: dict[str, set[str]] = {name: set() for name in paths}
    class Imports(ast.NodeVisitor):
        def __init__(self, name: str):
            self.name = name
        def visit_If(self, node: ast.If) -> None:
            if isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING':
                for child in node.orelse:
                    self.visit(child)
            else:
                self.generic_visit(node)
        def visit_Import(self, node: ast.Import) -> None:
            graph[self.name].update(alias.name for alias in node.names)
        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                graph[self.name].add(node.module)
    for name, path in paths.items():
        Imports(name).visit(ast.parse(path.read_text()))
    return graph


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return strongly connected components; no legacy-cycle exemptions."""
    index, indices, low, stack, active = 0, {}, {}, [], set()
    found: list[list[str]] = []
    def visit(name: str) -> None:
        nonlocal index
        indices[name] = low[name] = index; index += 1
        stack.append(name); active.add(name)
        for target in sorted(graph[name] & graph.keys()):
            if target not in indices:
                visit(target); low[name] = min(low[name], low[target])
            elif target in active:
                low[name] = min(low[name], indices[target])
        if low[name] == indices[name]:
            group = []
            while True:
                target = stack.pop(); active.remove(target); group.append(target)
                if target == name:
                    break
            if len(group) > 1 or name in graph[name]:
                found.append(sorted(group))
    for name in sorted(graph):
        if name not in indices:
            visit(name)
    return sorted(found)
