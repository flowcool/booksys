"""Neutral parser seams: source ambiguity never becomes an arbitrary match."""
import unittest
from library.french_metadata import parse_series_search, parse_series_albums, parse_album_page

BASE='https://www.bedetheque.com'

class FrenchMetadataTests(unittest.TestCase):
    def test_discovery_returns_all_plausible_series(self):
        body='<main><a href="/serie-1-BD-Geeks.html">Les Geeks</a><a href="/serie/index/s/2">Geeks Club</a></main>'
        self.assertEqual([r.reference_id for r in parse_series_search(body)],['1','2'])

    def test_album_keeps_roles_and_unknown_language_age_without_first_contributor_loss(self):
        body='''<html><h1><a href="/serie/index/s/7">Geeks</a></h1>
        <meta itemprop="name" content="Geeks - Album"><meta itemprop="alternativeheadline" content="Tome 4">
        <div class="liste-auteurs"><span><a>Writer, One</a><small>(Scénario)</small></span>
        <span><a>Writer, Two</a><small>(Scénario)</small></span><span><a>Artist</a><small>(Dessin)</small></span></div>
        <input id="EAN" value="9782302008168"><span itemprop="publisher">Soleil</span>
        <span itemprop="description">Summary <a>Lire la suite</a></span></html>'''
        candidate=parse_album_page(body,BASE+'/album/index/i/123','2026-11-08')
        self.assertEqual((candidate.title,candidate.series,candidate.number),('Album','Geeks','4'))
        self.assertEqual(candidate.fields['writers'],['One Writer','Two Writer'])
        self.assertIsNone(candidate.fields['language'])
        self.assertIsNone(candidate.fields['adult'])
        self.assertIsNone(candidate.fields['recommended_age'])
        self.assertEqual(candidate.fields['isbn'],'9782302008168')

    def test_series_rows_include_all_editions_integrals_and_unnumbered_without_guess(self):
        body='''<ul class="bdt-box-albums-liste"><li><a href="/BD-Series-Tome-1-Album-10.html"><span class="bdt-box-albums-titre">Album</span></a></li>
        <li><a href="/BD-Series-Tome-1-Album-11.html"><span class="bdt-box-albums-titre">Album</span></a></li>
        <li><a href="/album/index/i/12"><span class="bdt-box-albums-titre">Integral</span></a></li></ul>'''
        candidates=parse_series_albums(body,'Series','2026-11-08')
        self.assertEqual(len(candidates),3)
        self.assertEqual([c.number for c in candidates],['1','1',None])
        self.assertIsNone(candidates[2].fields['integral'])

    def test_explicit_source_genres_do_not_turn_adult_tag_into_numeric_age(self):
        candidate=parse_album_page('''<html><meta itemprop="name" content="Album">
            <meta itemprop="inLanguage" content="fr"><meta itemprop="genre" content="Franco-Belge - Érotique">
            <meta itemprop="alternativeheadline" content="Intégrale"></html>''',BASE+'/album/index/i/123','now')
        self.assertEqual(candidate.fields['genres'],['Érotique'])
        self.assertEqual(candidate.fields['family'],'BD')
        self.assertTrue(candidate.fields['adult'])
        self.assertIsNone(candidate.fields['recommended_age'])
        self.assertTrue(candidate.fields['integral'])

    def test_public_discovery_keeps_ambiguity_and_never_calls_forbidden_ajax(self):
        from library.french_metadata import discover
        from library.french_source import ProviderPage
        class Source:
            urls=[]
            def get(self,url):
                self.urls.append(url)
                return ProviderPage('ok',url,'<a href="/serie-1-BD-Geeks.html">Geeks</a><a href="/serie-2-BD-Geeks.html">Geeks</a>')
        source=Source()
        result=discover(source,'Geeks',max_series=1)
        self.assertEqual(result.status,'ambiguous')
        self.assertTrue(result.truncated)
        self.assertFalse(any('/ajax/' in url for url in source.urls))

    def test_robots_combines_repeated_wildcard_groups(self):
        from library.french_source import robots_allowed
        policy='User-agent: *\nAllow: /\nUser-agent: GPTBot\nDisallow: /\nUser-agent: *\nDisallow: /ajax/\nDisallow: /Couvertures/\n'
        self.assertFalse(robots_allowed(policy,BASE+'/ajax/series'))
        self.assertFalse(robots_allowed(policy,BASE+'/Couvertures/a.jpg'))
        self.assertTrue(robots_allowed(policy,BASE+'/serie-7-BD-Geeks.html'))

    def test_equivalent_series_article_form_is_removed_from_microdata_title(self):
        candidate=parse_album_page('''<h1><a href="/serie-17075-BD-Geeks.html">Les geeks</a></h1>
        <meta itemprop="name" content="Geeks (Les) - Un clavier azerty en vaut deux">
        <meta itemprop="alternativeheadline" content="Tome 1">''',BASE+'/album/index/i/71072','now')
        self.assertEqual(candidate.title,'Un clavier azerty en vaut deux')

    def test_retained_real_reference_fields_include_manga_integral_and_adult_unknown_age(self):
        import json
        from pathlib import Path
        root=Path(__file__).parent/'fixtures/french-metadata'
        provenance=json.loads((root/'provenance.json').read_text())
        parsed={}
        for name, record in provenance.items():
            candidate=parse_album_page((root/name).read_text(),record['url'],record['retrieved_at'])
            parsed[candidate.reference_id]=candidate
        self.assertEqual(parsed['71072'].fields['isbn'],'9782302000599')
        self.assertEqual(parsed['95136'].title,"Hacker vaillant rien d'impossible")
        self.assertIsNone(parsed['540371'].number)
        self.assertEqual(parsed['24065'].fields['family'],'Manga')
        self.assertTrue(parsed['83239'].fields['integral'])
        self.assertTrue(parsed['972'].fields['adult'])
        self.assertIsNone(parsed['972'].fields['recommended_age'])
        self.assertEqual(parsed['972'].fields['genres'],['Érotique','Science-fiction'])

    def test_visible_editions_preserve_conflicting_secondary_values_without_choosing(self):
        from library.french_metadata import parse_album_editions
        body='''<h1><a>Series</a></h1><meta itemprop="name" content="Series - Album">
        <meta itemprop="alternativeheadline" content="Tome 1"><span itemprop="publisher">First</span>
        <dl class="bdt-sheet--compact"><dt>Titre</dt><dd>Album</dd><dt>Identifiant</dt><dd>456</dd>
        <dt>Éditeur</dt><dd>Other</dd><dt>EAN/ISBN</dt><dd>9782302008168</dd></dl>'''
        editions=parse_album_editions(body,BASE+'/album/index/i/123','now')
        self.assertEqual(len(editions),2)
        self.assertEqual([c.fields['publisher'] for c in editions],['First','Other'])
        self.assertEqual({c.title for c in editions},{'Album'})

    def test_reference_urls_cannot_escape_to_another_site_or_invalid_port(self):
        from library.french_metadata import reference_url
        from library.catalog import LibraryError
        for url in ['https://www.bedetheque.com.evil/album/index/i/1',
                    'https://www.bedetheque.com:invalid/album/index/i/1','file:///etc/passwd']:
            with self.assertRaises(LibraryError):reference_url(url)

    def test_offline_cache_and_exhausted_budget_do_not_trigger_network(self):
        import tempfile
        from pathlib import Path
        from library.french_source import QualificationSource
        with tempfile.TemporaryDirectory() as folder:
            offline=QualificationSource(Path(folder))
            self.assertEqual(offline.get(BASE+'/serie-1-BD-Geeks.html').status,'unavailable')
            self.assertEqual(offline.request_count(),0)
            blocked=QualificationSource(Path(folder),live=True,max_requests=0)
            self.assertEqual(blocked.get(BASE+'/serie-1-BD-Geeks.html').status,'budget_exhausted')
            self.assertEqual(blocked.request_count(),0)
