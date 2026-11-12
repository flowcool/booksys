"""Which stages run on work_items. Default: none (the switch back is an empty list).

BOOKSYS_WORK_STAGES="intake,ocr" is read when a worker or a commit needs it. A stage is on exactly one system:
the old stage_tasks actor or the work_items actor. Never both (a migration sets the item states first).
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from library.primitives import LibraryError

if TYPE_CHECKING:
    from library.postgres import Database
    from library.work_items import WorkItems

SUPPORTED = frozenset({'intake', 'ocr', 'metadata', 'copies', 'routing', 'collection'})   # grows stage by stage, each with its gate G3 on live data
MAX_ATTEMPTS = {'intake': 3, 'ocr': 3, 'metadata': 3, 'copies': 3, 'routing': 3, 'collection': 3}  # same limit as the old tasks


def work_stages() -> frozenset[str]:
    names = frozenset(name for name in os.environ.get('BOOKSYS_WORK_STAGES', '').split(',') if name)
    unknown = names - SUPPORTED
    if unknown:
        raise LibraryError('Stage not yet supported on work_items: ' + ','.join(sorted(unknown)))
    return names


def work_items_for(database: Database) -> WorkItems:
    from library.work_items import WorkItems
    return WorkItems(database, max_attempts=dict(MAX_ATTEMPTS))
