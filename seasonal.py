"""Small weather-dependent decorations for the seasonal garden."""

from functools import lru_cache
import math
import random


BUTTERFLY = ((171, 183, 236), (243, 199, 124))
SEED = (225, 228, 207)
SEED_STEM = (159, 175, 137)
WATER = (69, 112, 140)
RIPPLE = (132, 183, 208)
POLE = (151, 129, 105)
PINWHEEL = ((129, 192, 198), (235, 190, 118), (211, 139, 129), (161, 197, 147))
MUSHROOM_CAP = (193, 112, 97)
MUSHROOM_STEM = (227, 208, 170)
ROLLING_LEAF = (242, 184, 91)
MIST = ((77, 92, 102), (94, 108, 119))


def draw_butterflies(canvas, elapsed):
    for index, (cx, cy) in enumerate(((47, 67), (108, 72))):
        x = cx + math.sin(elapsed * 0.45 + index * 2) * 10
        y = cy + math.sin(elapsed * 0.65 + index) * 4
        width = 1.6 + (math.sin(elapsed * 5 + index) + 1) * 0.8
        for side in (-1, 1):
            canvas.line(x, y, x + side * width, y - 2.6, BUTTERFLY[index], radius=0.2)
            canvas.line(x + side * width, y - 2.6, x + side * width, y - 0.3, BUTTERFLY[index])
            canvas.line(x + side * width, y - 0.3, x, y, BUTTERFLY[index])
            canvas.line(x, y, x + side * width * 0.7, y + 1.6, BUTTERFLY[index])
            canvas.line(x + side * width * 0.7, y + 1.6, x, y + 0.8, BUTTERFLY[index])
        canvas.line(x, y - 0.5, x, y + 1.4, POLE)
        canvas.point(x - 0.6, y - 1.4, POLE)
        canvas.point(x + 0.6, y - 1.4, POLE)


def draw_dandelions(canvas, elapsed):
    canvas.line(43, 99, 43, 93, SEED_STEM)
    for index in range(12):
        angle = index * math.tau / 12
        canvas.point(43 + math.cos(angle) * 2.3, 91.5 + math.sin(angle) * 2.3, SEED)
    for index in range(8):
        progress = (elapsed * 0.035 + index / 8) % 1
        x = 43 + progress * 94
        y = 90 - progress * 28 + math.sin(elapsed * 0.8 + index) * 2
        canvas.line(x, y, x - 1.1, y + 1.8, SEED_STEM)
        canvas.line(x - 1, y - 0.6, x + 1, y - 0.6, SEED)
        canvas.point(x, y - 1.2, SEED)


def ellipse(canvas, cx, cy, rx, ry, color, count=24):
    for index in range(count):
        angle = index * math.tau / count
        canvas.point(cx + rx * math.cos(angle), cy + ry * math.sin(angle), color)


def draw_puddles(canvas, elapsed):
    for index, cx in enumerate((49, 110)):
        cy = 102 + index * 0.4
        ellipse(canvas, cx, cy, 8, 1.7, WATER, 36)
        canvas.line(cx - 5.5, cy + 0.4, cx + 5.5, cy + 0.4, WATER)
        for offset in (0, 0.5):
            progress = (elapsed * 0.8 + index * 0.37 + offset) % 1
            ellipse(canvas, cx, cy, 1 + progress * 6, 0.25 + progress * 1.05,
                    RIPPLE if progress < 0.8 else WATER)


def draw_pinwheel(canvas, elapsed):
    cx, cy = 47, 76
    canvas.line(cx, 99, cx, cy, POLE, radius=0.2)
    for index, color in enumerate(PINWHEEL):
        angle = elapsed * 0.95 + index * math.pi / 2
        tip = (cx + math.cos(angle) * 4.7, cy + math.sin(angle) * 4.7)
        edge = (cx + math.cos(angle - 0.8) * 2.8, cy + math.sin(angle - 0.8) * 2.8)
        canvas.line(cx, cy, *tip, color)
        canvas.line(*tip, *edge, color, radius=0.2)
        canvas.line(*edge, cx, cy, color)
    canvas.point(cx, cy, MUSHROOM_STEM)


@lru_cache(maxsize=8)
def mushroom_shape(columns, rows):
    points = []
    for cx, base, radius in ((43, 100, 3.8), (52, 101, 2.6), (36.5, 101, 2.1)):
        cap_y, height = base - radius * 0.9, radius * 0.7
        for step in range(math.ceil((base - cap_y) * 2) + 1):
            for dx in (-0.35, 0, 0.35):
                points.append((cx + dx, base - step * 0.5, MUSHROOM_STEM))
        for step in range(math.ceil(height * 2) + 1):
            dy = min(height, step * 0.5)
            width = radius * math.sqrt(max(0, 1 - (dy / height) ** 2))
            for col in range(math.ceil(width * 4) + 1):
                points.append((cx - width + col * 0.5, cap_y - dy, MUSHROOM_CAP))
        points.extend(((cx - radius * 0.4, cap_y - height * 0.35, MUSHROOM_STEM),
                       (cx + radius * 0.35, cap_y - height * 0.55, MUSHROOM_STEM)))
    max_x, max_y = columns * 2 - 1, rows * 4 - 1
    return {(round(x / 160 * max_x), round(y / 112 * max_y)): color for x, y, color in points
            if 0 <= x <= 160 and 0 <= y <= 112}


def draw_mushrooms(canvas):
    canvas.pixels.update(mushroom_shape(canvas.columns, canvas.rows))


def draw_rolling_leaves(canvas, elapsed):
    for index in range(3):
        x = 32 + (elapsed * 5.5 + index * 31) % 98
        y = 100.5 - abs(math.sin(elapsed * 2 + index)) * 1.3
        angle = elapsed * 2.2 + index
        dx, dy = math.cos(angle) * 1.8, math.sin(angle) * 0.9
        canvas.line(x - dx, y - dy, x + dx, y + dy, ROLLING_LEAF, radius=0.25)
        canvas.point(x - dx * 1.4, y - dy * 1.4, POLE)


def fog_points():
    rng = random.Random(48)
    return tuple((rng.uniform(0, 120), rng.gauss(0, 1.4), rng.randrange(2)) for _ in range(130))


FOG_POINTS = fog_points()


def draw_fog(canvas, elapsed):
    for source_x, source_y, shade in FOG_POINTS:
        x = 20 + (source_x + elapsed * 0.6) % 120
        y = 92 + source_y + math.sin(x * 0.055 + elapsed * 0.16) * 1.6
        canvas.point(x, y, MIST[shade])
        if shade == 0:
            canvas.point(x + 0.7, y, MIST[shade])


def draw_accents(canvas, season, weather, elapsed):
    if season == "spring" and weather == "clear":
        draw_butterflies(canvas, elapsed)
    elif season == "spring" and weather == "wind":
        draw_dandelions(canvas, elapsed)
    elif season == "summer" and weather == "rain":
        draw_puddles(canvas, elapsed)
    elif season == "summer" and weather == "wind":
        draw_pinwheel(canvas, elapsed)
    elif season == "autumn" and weather == "rain":
        draw_mushrooms(canvas)
    elif season == "autumn" and weather == "wind":
        draw_rolling_leaves(canvas, elapsed)
