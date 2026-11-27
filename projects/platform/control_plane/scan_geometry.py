"""Keep scan corrections and table cells tied to the original raster page."""

from math import ceil, hypot


def deskew(image):
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    scale = min(1, 850 / max(height, width))
    reduced = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(reduced, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    # Ignore dark scan borders. Text and ruling both strengthen horizontal projections.
    margin = max(2, int(min(mask.shape) * 0.035))
    mask = mask[margin:-margin, margin:-margin]
    h, w = mask.shape
    center = (w / 2, h / 2)

    def score(angle):
        matrix = cv2.getRotationMatrix2D(center, angle, 1)
        rotated = cv2.warpAffine(mask, matrix, (w, h))
        rows = rotated.sum(axis=1, dtype=np.float64)
        return float(np.square(np.diff(rows)).sum())

    baseline = score(0)
    coarse = max(range(-5, 6), key=score)
    angle = round(float(max(np.linspace(coarse - .5, coarse + .5, 11), key=score)), 2)
    if abs(angle) < .25 or abs(angle) > 5 or score(angle) <= baseline * 1.12:
        return image, None, 0.0

    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    # Expand the canvas so the correction never clips content at a corner.
    a, b = map(abs, matrix[0, :2])
    out_w, out_h = ceil(width * a + height * b), ceil(width * b + height * a)
    matrix[:, 2] += ((out_w - width) / 2, (out_h - height) / 2)
    corrected = cv2.warpAffine(image, matrix, (out_w, out_h), borderValue=(255, 255, 255))
    return corrected, cv2.invertAffineTransform(matrix).tolist(), angle


def source_region(box, width, height, inverse):
    left, top, right, bottom = box
    points = [(left, top), (right, top), (right, bottom), (left, bottom)]
    if inverse is not None:
        (a, b, c), (d, e, f) = inverse
        points = [(a*x + b*y + c, d*x + e*y + f) for x, y in points]
    quad = [[round(min(1, max(0, x/width)), 6), round(min(1, max(0, y/height)), 6)]
            for x, y in points]
    xs, ys = zip(*quad)
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError('empty OCR region')
    return {'bbox': bounds, 'polygon': quad}


def ruled_table(image, box, lines):
    """Recover reading order only when whole separators support an unmerged grid."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    x1, y1, x2, y2 = map(int, box)
    pad = max(5, round(min(width, height) * .008))
    x1, y1, x2, y2 = max(0, x1-pad), max(0, y1-pad), min(width, x2+pad), min(height, y2+pad)
    if min(x2-x1, y2-y1) < 30:
        return None
    gray = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY_INV, 25, 9)
    h, w = binary.shape

    def separators(axis):
        horizontal = axis == 1
        size = (max(12, w//3), 1) if horizontal else (1, max(12, h//3))
        projected = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, size))
        indexes = np.flatnonzero((projected != 0).mean(axis=axis) >= .64)
        groups = np.split(indexes, np.flatnonzero(np.diff(indexes) > 2) + 1)
        return [int(group.mean()) for group in groups if group.size]

    xs = [x+x1 for x in separators(0)]
    ys = [y+y1 for y in separators(1)]
    if not (3 <= len(xs) <= 32 and 3 <= len(ys) <= 120):
        return None
    if min(np.diff(xs).min(), np.diff(ys).min()) < 9:
        return None
    grid = []
    for top, bottom in zip(ys, ys[1:]):
        row = []
        for left, right in zip(xs, xs[1:]):
            inside = []
            for (l, t, r, b), text, confidence in lines:
                if left < (l+r)/2 < right and top < (t+b)/2 < bottom:
                    if l < left-2 or r > right+2 or t < top-2 or b > bottom+2:
                        return None
                    checked_text = visible_numeric_brackets(image, (l, t, r, b), text)
                    inside.append((l, t, b, checked_text, confidence))
            # Text fragments on one baseline can have different ascender heights.
            # Group by vertical overlap before reading each line from the left.
            bands = []
            for fragment in sorted(inside, key=lambda item: (item[1] + item[2]) / 2):
                _, y0, y1, _, _ = fragment
                if bands:
                    low, high, members = bands[-1]
                    overlap = min(high, y1) - max(low, y0)
                    if overlap >= .5 * min(high - low, y1 - y0):
                        members.append(fragment)
                        continue
                bands.append((y0, y1, [fragment]))
            ordered = [item for _, _, members in bands for item in sorted(members, key=lambda f: f[0])]
            row.append({'text': ' '.join(item[3] for item in ordered),
                        'confidence': min((item[4] for item in ordered), default=None),
                        'box': [left, top, right, bottom]})
        grid.append(row)
    if not all(cell['text'] for cell in grid[0]) or not any(cell['text'] for row in grid[1:] for cell in row):
        return None
    return grid
def visible_numeric_brackets(image, box, text):
    """Recover angular brackets only when both printed outlines and digit spacing agree."""
    import re
    import numpy as np

    pairs = list(re.finditer(r'[(（]([0-9]+)[)）]', text))
    if len(pairs) != 1:
        return text
    match = pairs[0]
    left, top, right, bottom = map(int, box)
    crop = image[max(top-2, 0):bottom+2, max(left-2, 0):right+2]
    if not crop.size or crop.shape[0] < 15:
        return text
    ink = crop.mean(axis=2) < 155
    occupied = np.flatnonzero(ink.any(axis=0))
    if not occupied.size:
        return text
    glyphs = np.split(occupied, np.flatnonzero(np.diff(occupied) > 1) + 1)

    def angular(group, closing):
        glyph = ink[:, group[0]:group[-1]+1]
        rows = np.flatnonzero(glyph.any(axis=1))
        glyph = glyph[rows[0]:rows[-1]+1]
        height, width = glyph.shape
        if height < 16 or not 3 <= width <= height * .32 or not glyph.any(axis=1).all():
            return False
        if closing:
            glyph = glyph[:, ::-1]
        spine = glyph.argmax(axis=1)
        core = spine[round(height*.22):round(height*.78)]
        baseline = np.median(core)
        if core.max()-core.min() > 1 or np.mean(core == baseline) < .82:
            return False
        # Square brackets have horizontal end strokes, round brackets a curved spine.
        cap = max(2, round(height*.19))
        upper, lower = spine[:cap], spine[-cap:]
        return (upper[0]-baseline >= 2 and lower[-1]-baseline >= 2 and
                upper[-1] <= baseline+1 and lower[0] <= baseline+1 and
                len(set(upper)) >= 3 and len(set(lower)) >= 3 and
                glyph.sum(axis=1).max() < width)

    offset = len(match[1]) + 1
    possible = [index for index in range(len(glyphs)-offset)
                if angular(glyphs[index], False) and angular(glyphs[index+offset], True)]
    if len(possible) != 1:
        return text
    # A second numeric group could otherwise be assigned the first group's shapes.
    if any(char in text[:match.start()]+text[match.end():] for char in '()（）〔〕'):
        return text
    return text[:match.start()] + '〔' + match[1] + '〕' + text[match.end():]

