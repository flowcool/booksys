"""Cover similarity proposals from a pinned ComicTagger component; no identity decision."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PIL import Image, ImageStat

from library._vendor.comictagger_imagehasher import ImageHasher

METHOD = 'comictagger-fa11a345-phash64-v1'


def cover_hash(image: Image.Image) -> int | None:
    """Hash an already decoded, validated cover; caller owns file/image isolation.

    Flat or very small images provide no distinguishing cover evidence. Do not
    use the upstream decoder fallback, which substitutes a blank image on errors.
    """
    if min(image.size) < 8:
        return None
    grey = image.convert('L')
    if ImageStat.Stat(grey).var[0] == 0:
        return None
    return ImageHasher(image=image).perception_hash()


def compare_covers(local: int | None, candidates: Mapping[str, int | None]) -> list[dict[str, Any]]:
    """Return every usable candidate, including ties; distance is not confidence.

    No score threshold, automatic winner, edition or age inference is imposed.
    An integrator must combine these proposals with independent work evidence.
    """
    if local is None:
        return []
    return sorted(({'reference_id': key, 'distance': ImageHasher.hamming_distance(local, value)}
                   for key, value in candidates.items() if value is not None),
                  key=lambda item: (item['distance'], item['reference_id']))
