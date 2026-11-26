"""Child-only bounded SIFT adapter.

SIFT matching/scoring adapted from bdnex 0.10.0, commit
9de29a73859a527a0c8ab4b0173d2f1e0e1e6f33, importer/covers.py.
Author: Laurent Besnard. The distributed LICENSE is CC0-1.0 (its
package metadata says MIT). See third_party/bdnex-cover-LICENSE.txt.
Changes: bounded header inspection, upstream 600px-height preprocessing, fixed RNG,
one native thread, normalized-input digests and resource measurements.
"""
from __future__ import annotations

import hashlib
import io
import math
import resource
import time
from typing import Any

from PIL import Image

METHOD = 'bdnex-9de29a73-sift-height600-ratio06-v1'
FUNCTION_VERSION = 'bdnex-0.10.0-9de29a73-sift'
PREPROCESSING = 'opencv-gray-height600-inter-area-v1'
THRESHOLD = 40.0  # Provisional operator-approved threshold. Do not tune implicitly.
MAX_PIXELS = 25_000_000
SIFT_HEIGHT = 600


def similarity(original: bytes, other: bytes) -> dict[str, Any]:
    """Execute only inside reader_worker, after its per-call limits are set."""
    started, cpu = time.monotonic(), time.process_time()
    # Inspect dimensions before OpenCV allocates decoded pixels.
    for data in (original, other):
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise ValueError('Cover pixel limit exceeded')
            if image.format not in ('JPEG', 'PNG', 'WEBP', 'GIF'):
                raise ValueError('Unsupported cover image')
    import cv2
    import numpy
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    cv2.setRNGSeed(0)

    def grey(data: bytes) -> Any:
        picture = cv2.imdecode(numpy.frombuffer(data, numpy.uint8), cv2.IMREAD_GRAYSCALE)
        if picture is None:
            raise ValueError('Unreadable cover image')
        rows, cols = picture.shape[:2]
        height = SIFT_HEIGHT
        width = max(1, round(cols * height / rows))
        if width * height > MAX_PIXELS:
            raise ValueError('Normalized cover pixel limit exceeded')
        return cv2.resize(picture, (width, height),
                          interpolation=cv2.INTER_AREA)

    first, second = grey(original), grey(other)
    sift = cv2.SIFT_create()  # type: ignore[attr-defined]  # Wheel stubs omit this exported API.
    points_a, described_a = sift.detectAndCompute(first, None)
    points_b, described_b = sift.detectAndCompute(second, None)
    score = 0.0
    if (described_a is not None and described_b is not None
            and len(described_a) >= 2 and len(described_b) >= 2):
        matcher = cv2.FlannBasedMatcher({'algorithm': 1, 'trees': 5}, {})
        pairs = matcher.knnMatch(described_a, described_b, k=2)
        good = sum(1 for pair in pairs if len(pair) == 2 and pair[0].distance < 0.6 * pair[1].distance)
        score = min(100.0, 100.0 * good / min(len(points_a), len(points_b)))
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError('Invalid SIFT score')
    return {'score': score, 'opencv': cv2.__version__, 'numpy': numpy.__version__,
            'normalized_input_sha256': hashlib.sha256(first.tobytes()).hexdigest(),
            'normalized_reference_sha256': hashlib.sha256(second.tobytes()).hexdigest(),
            'wall_seconds': time.monotonic() - started, 'cpu_seconds': time.process_time() - cpu,
            'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
