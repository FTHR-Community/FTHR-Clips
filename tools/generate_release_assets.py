#!/usr/bin/env python3
"""Generate release art from geometric primitives and an original bitmap font.

Source UI assets and Oswald are retained. Redistribution terms are in
licenses/FTHR-GENERATED-ASSETS.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import struct
import sys
import wave
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GENERATOR_VERSION = 1

TRANSPARENT = (0, 0, 0, 0)
BLACK = (8, 11, 16, 255)
PANEL = (20, 25, 33, 255)
WHITE = (240, 244, 249, 255)
MUTED = (139, 149, 161, 255)
ACCENT = (255, 75, 160, 255)
GRID = (23, 29, 38, 255)
PALETTE = (TRANSPARENT, BLACK, PANEL, WHITE, MUTED, ACCENT, GRID)
INSTALLER_BLACK = (0, 0, 0, 255)
INSTALLER_ACCENT = (0, 255, 170, 255)


def _stored_zlib(data: bytes) -> bytes:
    """Return a platform-independent zlib stream of stored DEFLATE blocks."""
    stream = bytearray(b'\x78\x01')
    offset = 0
    while offset < len(data):
        block = data[offset:offset + 65_535]
        offset += len(block)
        stream.append(1 if offset == len(data) else 0)
        stream.extend(struct.pack('<HH', len(block), 0xffff - len(block)))
        stream.extend(block)
    if not data:
        stream.extend(b'\x01\x00\x00\xff\xff')
    stream.extend(struct.pack('>I', zlib.adler32(data) & 0xffffffff))
    return bytes(stream)


class Canvas:
    """Small deterministic RGBA rasterizer for the project's basic shapes."""

    def __init__(self, width: int, height: int, color=TRANSPARENT):
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def pixel(self, x: int, y: int, color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 4
            self.pixels[i:i + 4] = bytes(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        x0, x1 = sorted((max(0, x0), min(self.width, x1)))
        y0, y1 = sorted((max(0, y0), min(self.height, y1)))
        row = bytes(color) * max(0, x1 - x0)
        for y in range(y0, y1):
            i = (y * self.width + x0) * 4
            self.pixels[i:i + len(row)] = row

    def line(self, x0: int, y0: int, x1: int, y1: int, color,
             thickness: int = 1) -> None:
        dx = x1 - x0
        dy = y1 - y0
        steps = max(abs(dx), abs(dy), 1)
        radius = max(0, thickness // 2)
        for step in range(steps + 1):
            x = round(x0 + dx * step / steps)
            y = round(y0 + dy * step / steps)
            self.rect(x - radius, y - radius,
                      x + radius + 1, y + radius + 1, color)

    def polygon(self, points: list[tuple[int, int]], color) -> None:
        min_y = max(0, min(y for _, y in points))
        max_y = min(self.height - 1, max(y for _, y in points))
        for y in range(min_y, max_y + 1):
            intersections = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if y1 == y2 or not (min(y1, y2) <= y < max(y1, y2)):
                    continue
                intersections.append(round(x1 + (y - y1) * (x2 - x1) /
                                           (y2 - y1)))
            intersections.sort()
            for x0, x1 in zip(
                    intersections[::2], intersections[1::2], strict=True):
                self.rect(x0, y, x1 + 1, y + 1, color)

    def ellipse(self, cx: int, cy: int, rx: int, ry: int, color,
                thickness: int = 0) -> None:
        inner_rx = max(0, rx - thickness)
        inner_ry = max(0, ry - thickness)
        for y in range(max(0, cy - ry), min(self.height, cy + ry + 1)):
            for x in range(max(0, cx - rx), min(self.width, cx + rx + 1)):
                outer = ((x - cx) / max(rx, 1)) ** 2 + \
                        ((y - cy) / max(ry, 1)) ** 2 <= 1
                if not outer:
                    continue
                inner = (thickness and inner_rx and inner_ry and
                         ((x - cx) / inner_rx) ** 2 +
                         ((y - cy) / inner_ry) ** 2 < 1)
                if not inner:
                    self.pixel(x, y, color)

    def arc(self, cx: int, cy: int, radius: int, start: float, end: float,
            color, thickness: int) -> None:
        count = max(8, round(abs(end - start) * radius / 6))
        previous = None
        for index in range(count + 1):
            angle = start + (end - start) * index / count
            point = (round(cx + math.cos(angle) * radius),
                     round(cy + math.sin(angle) * radius))
            if previous:
                self.line(*previous, *point, color, thickness)
            previous = point

    def text(self, x: int, y: int, value: str, scale: int, color,
             spacing: int = 1) -> None:
        cursor = x
        for char in value.upper():
            rows = GLYPHS.get(char, GLYPHS['?'])
            for row, bits in enumerate(rows):
                for column, bit in enumerate(bits):
                    if bit == '1':
                        self.rect(cursor + column * scale, y + row * scale,
                                  cursor + (column + 1) * scale,
                                  y + (row + 1) * scale, color)
            cursor += (5 + spacing) * scale

    def png(self) -> bytes:
        palette_index = {color: index for index, color in enumerate(PALETTE)}
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)
            for x in range(self.width):
                start = (y * self.width + x) * 4
                color = tuple(self.pixels[start:start + 4])
                raw.append(palette_index[color])

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack('>I', len(data)) + kind + data +
                    struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

        return (b'\x89PNG\r\n\x1a\n' +
                chunk(b'IHDR', struct.pack('>IIBBBBB', self.width, self.height,
                                           8, 3, 0, 0, 0)) +
                chunk(b'sRGB', b'\x00') +
                chunk(b'PLTE', b''.join(bytes(color[:3])
                                       for color in PALETTE)) +
                chunk(b'tRNS', bytes(color[3] for color in PALETTE)) +
                chunk(b'IDAT', _stored_zlib(bytes(raw))) +
                chunk(b'IEND', b''))

    def bmp(self) -> bytes:
        row_size = ((self.width * 3 + 3) // 4) * 4
        image_size = row_size * self.height
        header = (b'BM' + struct.pack('<IHHI', 54 + image_size, 0, 0, 54) +
                  struct.pack('<IiiHHIIiiII', 40, self.width, self.height,
                              1, 24, 0, image_size, 3780, 3780, 0, 0))
        body = bytearray()
        for y in range(self.height - 1, -1, -1):
            row = bytearray()
            for x in range(self.width):
                i = (y * self.width + x) * 4
                r, g, b = self.pixels[i:i + 3]
                row.extend((b, g, r))
            row.extend(b'\x00' * (row_size - len(row)))
            body.extend(row)
        return header + bytes(body)


# Original compact 5x7 bitmap alphabet used only by generated project art.
GLYPHS = {
    ' ': ('00000',) * 7,
    '?': ('01110', '10001', '00010', '00100', '00100', '00000', '00100'),
    '-': ('00000', '00000', '00000', '11111', '00000', '00000', '00000'),
    '.': ('00000', '00000', '00000', '00000', '00000', '00110', '00110'),
    '+': ('00000', '00100', '00100', '11111', '00100', '00100', '00000'),
    '0': ('01110', '10001', '10011', '10101', '11001', '10001', '01110'),
    '1': ('00100', '01100', '00100', '00100', '00100', '00100', '01110'),
    '2': ('01110', '10001', '00001', '00010', '00100', '01000', '11111'),
    '3': ('11110', '00001', '00001', '01110', '00001', '00001', '11110'),
    '4': ('00010', '00110', '01010', '10010', '11111', '00010', '00010'),
    '5': ('11111', '10000', '10000', '11110', '00001', '00001', '11110'),
    '6': ('01110', '10000', '10000', '11110', '10001', '10001', '01110'),
    '7': ('11111', '00001', '00010', '00100', '01000', '01000', '01000'),
    '8': ('01110', '10001', '10001', '01110', '10001', '10001', '01110'),
    '9': ('01110', '10001', '10001', '01111', '00001', '00001', '01110'),
    'A': ('01110', '10001', '10001', '11111', '10001', '10001', '10001'),
    'B': ('11110', '10001', '10001', '11110', '10001', '10001', '11110'),
    'C': ('01111', '10000', '10000', '10000', '10000', '10000', '01111'),
    'D': ('11110', '10001', '10001', '10001', '10001', '10001', '11110'),
    'E': ('11111', '10000', '10000', '11110', '10000', '10000', '11111'),
    'F': ('11111', '10000', '10000', '11110', '10000', '10000', '10000'),
    'G': ('01111', '10000', '10000', '10111', '10001', '10001', '01111'),
    'H': ('10001', '10001', '10001', '11111', '10001', '10001', '10001'),
    'I': ('11111', '00100', '00100', '00100', '00100', '00100', '11111'),
    'J': ('00111', '00010', '00010', '00010', '10010', '10010', '01100'),
    'K': ('10001', '10010', '10100', '11000', '10100', '10010', '10001'),
    'L': ('10000', '10000', '10000', '10000', '10000', '10000', '11111'),
    'M': ('10001', '11011', '10101', '10101', '10001', '10001', '10001'),
    'N': ('10001', '11001', '10101', '10011', '10001', '10001', '10001'),
    'O': ('01110', '10001', '10001', '10001', '10001', '10001', '01110'),
    'P': ('11110', '10001', '10001', '11110', '10000', '10000', '10000'),
    'Q': ('01110', '10001', '10001', '10001', '10101', '10010', '01101'),
    'R': ('11110', '10001', '10001', '11110', '10100', '10010', '10001'),
    'S': ('01111', '10000', '10000', '01110', '00001', '00001', '11110'),
    'T': ('11111', '00100', '00100', '00100', '00100', '00100', '00100'),
    'U': ('10001', '10001', '10001', '10001', '10001', '10001', '01110'),
    'V': ('10001', '10001', '10001', '10001', '10001', '01010', '00100'),
    'W': ('10001', '10001', '10001', '10101', '10101', '10101', '01010'),
    'X': ('10001', '10001', '01010', '00100', '01010', '10001', '10001'),
    'Y': ('10001', '10001', '01010', '00100', '00100', '00100', '00100'),
    'Z': ('11111', '00001', '00010', '00100', '01000', '10000', '11111'),
}


def draw_mark(size: int, background=TRANSPARENT, accent=ACCENT) -> Canvas:
    canvas = Canvas(size, size, background)
    unit = size / 16
    thick = max(2, round(unit * 1.15))
    pad = round(unit * 2.5)
    length = round(unit * 3.5)
    # Four recording-frame corners.
    for x_sign, y_sign in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x = pad if x_sign > 0 else size - pad
        y = pad if y_sign > 0 else size - pad
        canvas.line(x, y, x + x_sign * length, y, WHITE, thick)
        canvas.line(x, y, x, y + y_sign * length, WHITE, thick)
    # A deliberately asymmetric original play/clip glyph.
    canvas.polygon([
        (round(unit * 6.3), round(unit * 4.8)),
        (round(unit * 11.8), round(unit * 8.0)),
        (round(unit * 6.3), round(unit * 11.2)),
        (round(unit * 7.7), round(unit * 8.0)),
    ], accent)
    return canvas


def draw_icon(name: str) -> Canvas:
    c = Canvas(128, 128)
    color = WHITE
    t = 9
    if name == 'clip.png':
        c.rect(20, 25, 108, 35, color); c.rect(20, 25, 30, 88, color)
        c.rect(98, 25, 108, 88, color); c.rect(20, 78, 108, 88, color)
        c.rect(58, 86, 70, 105, color); c.rect(42, 102, 86, 112, color)
    elif name == 'close.png':
        c.line(28, 28, 100, 100, color, t); c.line(100, 28, 28, 100, color, t)
    elif name == 'dropdown.png':
        c.line(30, 48, 64, 82, color, t); c.line(64, 82, 98, 48, color, t)
    elif name == 'home.png':
        c.polygon([(18, 61), (64, 20), (110, 61), (99, 72), (64, 41), (29, 72)], color)
        c.rect(31, 62, 97, 108, color); c.rect(55, 78, 73, 108, TRANSPARENT)
    elif name == 'maximize.png':
        c.rect(24, 24, 104, 34, color); c.rect(24, 94, 104, 104, color)
        c.rect(24, 24, 34, 104, color); c.rect(94, 24, 104, 104, color)
    elif name == 'minimize.png':
        c.rect(25, 84, 103, 95, color)
    elif name == 'pause.png':
        c.rect(34, 24, 53, 104, color); c.rect(75, 24, 94, 104, color)
    elif name == 'personalize.png':
        c.ellipse(64, 59, 42, 35, color)
        for x, y, r in ((43, 48, 7), (64, 39, 7), (84, 52, 7)):
            c.ellipse(x, y, r, r, TRANSPARENT)
        c.ellipse(82, 81, 14, 14, TRANSPARENT)
        c.line(81, 82, 105, 106, color, 8)
    elif name == 'play.png':
        c.polygon([(38, 22), (106, 64), (38, 106)], color)
    elif name == 'refresh.png':
        c.arc(64, 64, 38, math.radians(35), math.radians(315), color, t)
        c.polygon([(92, 30), (111, 34), (102, 51)], color)
    elif name == 'settings(general).png':
        for y, knob in ((35, 82), (64, 46), (93, 72)):
            c.line(20, y, 108, y, color, 7); c.ellipse(knob, y, 10, 10, color)
    elif name == 'sound.png':
        c.polygon([(20, 51), (42, 51), (66, 30), (66, 98), (42, 77), (20, 77)], color)
        c.arc(66, 64, 27, -0.75, 0.75, color, 7)
        c.arc(66, 64, 43, -0.75, 0.75, color, 7)
    elif name == 'shutdown.png':
        c.arc(64, 68, 39, math.radians(-55), math.radians(235), color, t)
        c.line(64, 17, 64, 68, color, t)
    elif name == 'updates.png':
        c.rect(57, 20, 71, 78, color)
        c.polygon([(32, 66), (96, 66), (64, 104)], color)
    elif name == 'visuals.png':
        c.polygon([(15, 64), (36, 38), (64, 27), (92, 38), (113, 64),
                   (92, 90), (64, 101), (36, 90)], color)
        c.ellipse(64, 64, 28, 28, TRANSPARENT); c.ellipse(64, 64, 13, 13, color)
    else:
        raise ValueError(f'unknown icon: {name}')
    return c


def social_preview() -> Canvas:
    c = Canvas(1280, 640, BLACK)
    # Original grid, panel and accent treatment, all rasterized here.
    for x in range(0, 1280, 80): c.line(x, 0, x, 640, GRID)
    for y in range(0, 640, 80): c.line(0, y, 1280, y, GRID)
    c.rect(0, 0, 8, 640, ACCENT)
    c.rect(58, 72, 824, 565, PANEL)
    c.rect(858, 72, 1222, 565, PANEL)
    mark = draw_mark(150)
    blit(c, mark, 82, 94)
    c.text(92, 285, 'FTHR CLIPS', 10, WHITE)
    c.rect(92, 380, 486, 388, ACCENT)
    c.text(92, 414, 'INSTANT REPLAY', 4, MUTED)
    c.text(92, 458, 'WINDOWS + LINUX', 4, MUTED)
    for y, key, label in ((130, 'F9', 'SAVE CLIP'), (280, 'F12', 'SCREENSHOT'),
                          (430, 'F7', 'DISMISS')):
        c.rect(890, y, 968, y + 48, ACCENT if key == 'F9' else MUTED)
        c.text(900, y + 13, key, 3, BLACK)
        c.text(980, y + 10, label, 4, WHITE if key == 'F9' else MUTED)
    c.text(24, 608, 'FTHR COMMUNITY', 3, MUTED)
    c.text(1060, 608, 'GPLV3 SOURCE', 3, WHITE)
    return c


def blit(target: Canvas, source: Canvas, x0: int, y0: int) -> None:
    for y in range(source.height):
        for x in range(source.width):
            i = (y * source.width + x) * 4
            color = tuple(source.pixels[i:i + 4])
            if color[3]:
                target.pixel(x0 + x, y0 + y, color)


def installer_banner(width: int, height: int) -> Canvas:
    c = Canvas(width, height, INSTALLER_BLACK)
    c.rect(width - 2, 0, width, height, INSTALLER_ACCENT)
    mark_size = min(width - 28, 112)
    mark = draw_mark(mark_size, accent=INSTALLER_ACCENT)
    blit(c, mark, (width - mark_size) // 2, max(8, height // 7))
    return c


def ico(images: list[Canvas]) -> bytes:
    payloads = [image.png() for image in images]
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for image, payload in zip(images, payloads, strict=True):
        width = image.width if image.width < 256 else 0
        height = image.height if image.height < 256 else 0
        entries.extend(struct.pack('<BBBBHHII', width, height, 0, 0, 1, 32,
                                   len(payload), offset))
        offset += len(payload)
    return struct.pack('<HHH', 0, 1, len(images)) + bytes(entries) + b''.join(payloads)


def sound(notes: list[tuple[float, float]], gap: float = 0.025) -> bytes:
    rate = 44_100
    frames: list[int] = []
    for frequency, duration in notes:
        count = round(duration * rate)
        fade = max(1, min(round(0.02 * rate), count // 4))
        for index in range(count):
            envelope = min(1.0, index / fade, (count - 1 - index) / fade)
            value = math.sin(2 * math.pi * frequency * index / rate)
            frames.append(round(11_000 * envelope * value))
        frames.extend([0] * round(gap * rate))
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f'<{len(frames)}h', *frames))
    return output.getvalue()


def generated_files() -> dict[Path, bytes]:
    logo = draw_mark(512).png()
    # The repository social preview (social_preview()) is no longer committed:
    # upstream removed .github/social_preview.png in 2.0.0-alpha. Regenerate it
    # by hand if the GitHub repository card needs it again.
    files = {
        ROOT / 'AppDir' / 'fthr-clips.png': logo,
        ROOT / 'installer_assets' / 'wizard_banner.bmp':
            installer_banner(164, 314).bmp(),
        ROOT / 'installer_assets' / 'wizard_small.bmp':
            installer_banner(55, 58).bmp(),
    }
    return files


def installer_files() -> dict[Path, bytes]:
    """Return only the installer artwork without touching other release art."""
    return {
        ROOT / 'installer_assets' / 'wizard_banner.bmp':
            installer_banner(164, 314).bmp(),
        ROOT / 'installer_assets' / 'wizard_small.bmp':
            installer_banner(55, 58).bmp(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='fail if committed generated files differ')
    parser.add_argument('--installer-only', action='store_true',
                        help='generate/check only the Windows installer artwork')
    args = parser.parse_args()
    mismatches = []
    expected_files = installer_files() if args.installer_only else generated_files()
    for path, expected in expected_files.items():
        relative = path.relative_to(ROOT)
        if args.check:
            actual = path.read_bytes() if path.is_file() else None
            if actual != expected:
                mismatches.append(str(relative))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        digest = hashlib.sha256(expected).hexdigest()
        print(f'{digest}  {relative.as_posix()}')
    if mismatches:
        for path in mismatches:
            print(f'generated asset mismatch: {path}', file=sys.stderr)
        return 1
    if args.check:
        print(f'{len(expected_files)} generated assets match version '
              f'{GENERATOR_VERSION}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
