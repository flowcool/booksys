"""Offline PR 51 evidence guards; no upstream package or transport is needed."""
from __future__ import annotations

import unittest
from typing import Any

from bdnex_service.engine import Engine
from library.asset_evidence import bind_evidence
from library.bdnex import identity_for
from library.names import parse_name
from library.work_identity import evaluate_work


def asset_state(name: str, *, embedded: dict[str, str] | None = None,
                heading: str | None = None) -> tuple[dict[str, Any], str]:
    key = 'a' * 64
    asset: dict[str, Any] = {'sha256': key, 'origins': ['o'], 'embedded': embedded or {}}
    if heading is not None:
        asset['captured_hints'] = {'fixture': {'title': heading}}
    return {'assets': {key: asset}, 'origins': {'o': {
        'sha256': key, 'path': name, 'source': 'fixture'}}}, key


class SeriesQualifierTests(unittest.TestCase):
    def test_rent_spin_off_qualifier_survives_filename_and_capture(self) -> None:
        full = ('Rent a (Really Shy) Girlfriend T02 (Miyajima) (2023) '
                '[Digital-1920u] [Manga FR] ....cbz')
        candidate = {'title': 'Tome 2', 'series': 'Rent-a-Girlfriend', 'number': '2'}
        for name, heading in ((full, None), (full, 'Rent-a-(Really Shy!)-Girlfriend - T02'),
                              ('Rent-a-(Really Shy!)-Girlfriend - T02.cbz', None),
                              ('Rent a (Really Shy) Girlfriend T02.cbz', None)):
            with self.subTest(name=name, heading=heading):
                state, key = asset_state(name, heading=heading)
                evidence = bind_evidence(state, key)
                self.assertNotEqual(evaluate_work(evidence, [candidate])['status'], 'identified')
                if heading or name != full:
                    self.assertIn('really shy', identity_for(state, key)['series'][0])
                    correct = {**candidate, 'series': 'Rent-a-(Really Shy!)-Girlfriend'}
                    self.assertEqual(evaluate_work(evidence, [correct])['status'], 'identified')

    def test_parenthetical_series_is_preserved_for_each_numbered_shape(self) -> None:
        for suffix in (' T02', ' - T02', ' - 02', ' #02', ' - 2. Tome 2'):
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_name('Rent-a-(Really Shy!)-Girlfriend' + suffix),
                                 {'series': 'Rent-a-(Really Shy!)-Girlfriend', 'number': '2'})

    def test_bernard_prince_title_veto_survives_equal_series_and_number(self) -> None:
        state, key = asset_state('Bernard.Prince.T15.cbz',
                                 embedded={'Title': 'Le Piège aux 100.000 dards'})
        candidate = {'title': 'Orage sur le Cormoran', 'series': 'Bernard Prince', 'number': '15'}
        result = evaluate_work(bind_evidence(state, key), [candidate])
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['rejected_fields'], ['title'])

    def test_my_greatest_adventure_dated_series_requires_a_cover_proposal(self) -> None:
        state, key = asset_state('My Greatest Adventure_03@BD_Fr.cbz')
        candidate = {'source': 'bedetheque', 'edition_ref': 'fixture-3', 'score': 80,
                     'series': 'My greatest adventure Vol1 DC comics 1955',
                     'title': 'Tome 3', 'number': '3'}
        # Exercise the real decision method without loading BDneX or a sitemap.
        engine = object.__new__(Engine)
        engine.numbered = {'3': [('My-greatest-adventure-Vol1-DC-comics-1955', 'fixture-3')]}
        self.assertEqual(engine.decide([candidate], identity_for(state, key)),
                         {'status': 'proposed', 'reason': 'dated_unique', 'edition_ref': 'fixture-3'})
        reprint, reprint_key = asset_state('My Greatest Adventure_03@BD_Fr.cbz', embedded={'Year': '2011'})
        self.assertEqual(engine.decide([candidate], identity_for(reprint, reprint_key))['status'], 'review')
        undated = {**candidate, 'series': 'My greatest adventure Vol1 DC comics'}
        engine.numbered = {'3': [('My-greatest-adventure-Vol1-DC-comics', 'fixture-3')]}
        self.assertEqual(engine.decide([undated], identity_for(state, key)),
                         {'status': 'selected', 'reason': 'numbered', 'edition_ref': 'fixture-3'})
        result = evaluate_work(bind_evidence(state, key), [candidate])
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['candidate_concordance']['mode'], 'none')
