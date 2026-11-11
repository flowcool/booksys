"""Compatibility exports. Use views for rendering and http for routing."""
from library.views import read_yaml, build_projection, fiche_html, PAGE
from library.presentation import bounded, source_url, fields, description_text, operator_reason
from library.http import handler

__all__ = ['read_yaml', 'bounded', 'source_url', 'fields', 'description_text',
           'operator_reason', 'build_projection', 'fiche_html', 'PAGE', 'handler']
