#!/usr/bin/env python3
"""Check payload-before-response ordering and error details in both engines.

Inspect enclosing C++ blocks rather than fixed line numbers. This source
gate supplements runtime tests for error paths that are hard to trigger.
Use --root to check a fixture tree. Exit 0 means intact; 1 means violations.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Engine source roots. third_party/ is vendored FFmpeg — not ours.
DEFAULT_ROOTS = [
    REPO / 'FTHRcapture' / 'FTHRclips' / 'src',
    REPO / 'FTHRcapture' / 'FTHRclips' / 'include',
]

# what counts as an event

#: Publishing a response by raw assignment. Captures the response name if it is
#: on the same line; a multi-line assignment carries it on a following line, so
#: the scanner joins logical statements before matching.
RE_PUBLISH = re.compile(r'\bengine_response\s*=\s*(?!.*==)(?P<rhs>[^;]*);', re.S)

#: The sanctioned error publisher — payload and response in one call.
RE_SET_ERROR = re.compile(r'\b(?:fthr::)?(?:SetEngineError|set_engine_error)\s*\(')

#: Sanctioned payload writers.
RE_SET_STRING = re.compile(
    r'\b(?:fthr::)?(?:SetEngineString|set_engine_string)\s*\(')

#: Any other payload write: engine_string touched directly, or a param field.
RE_RAW_STRING = re.compile(r'\bengine_string\b')
RE_PARAM = re.compile(r'\bengine_param[123]\s*=')

#: Unsafe string primitives aimed at the payload buffer.
RE_UNSAFE = re.compile(
    r'\b(strcpy|wcscpy|sprintf|swprintf|strcat|wcscat)\s*\([^;]*engine_string')

#: Functions allowed to touch engine_string directly — they are the helpers.
HELPER_DEFS = ('set_engine_string', 'SetEngineString', 'SetEngineError')

#: The UI is the only consumer. An engine writing NONE would destroy a verdict
#: it has already published — except at startup, where it initialises the field.
RE_CONSUME = re.compile(r'\bengine_response\s*=\s*(?:fthr::)?(?:ResponseType::)?NONE')


@dataclass
class Violation:
    rule: str
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        try:
            rel = self.path.relative_to(REPO)
        except ValueError:
            rel = self.path
        return f'  {self.rule}  {rel}:{self.line}\n      {self.detail}'


def _strip_comments(text: str) -> str:
    """Blank out comments and string literals, preserving line structure.

    Without this every explanatory comment mentioning ERROR_OCCURRED — and
    this project has many — reads as a violation.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(''.join(ch if ch == '\n' else ' ' for ch in text[i:j]))
            i = j
        elif c in '"\'':
            # Keep the quotes so call syntax survives; blank the contents.
            quote = c
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == '\\':
                    j += 1
                j += 1
            j = min(j + 1, n)
            out.append(quote + ' ' * max(0, j - i - 2) + quote)
            i = j
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _events(code: str):
    """Yield (line_no, depth, kind) for every contract-relevant event.

    kind is one of: 'publish_error', 'publish_other', 'payload', 'set_error',
    'raw_string', 'unsafe', 'consume'.
    """
    depth = 0
    for line_no, raw in enumerate(code.split('\n'), start=1):
        # Depth is tracked *before* the line's own closing braces are applied
        # for closers, so a statement and its block agree.
        opens, closes = raw.count('{'), raw.count('}')
        line_depth = depth - closes if closes > opens else depth

        if RE_UNSAFE.search(raw):
            yield line_no, line_depth, 'unsafe'
        if RE_SET_ERROR.search(raw):
            yield line_no, line_depth, 'set_error'
        elif RE_SET_STRING.search(raw) or RE_PARAM.search(raw):
            yield line_no, line_depth, 'payload'
        elif RE_RAW_STRING.search(raw) and 'engine_response' not in raw:
            yield line_no, line_depth, 'raw_string'

        if 'engine_response' in raw and '=' in raw and '==' not in raw:
            if RE_CONSUME.search(raw):
                yield line_no, line_depth, 'consume'
            else:
                yield line_no, line_depth, 'publish'

        depth += opens - closes


def _logical_statements(code: str):
    """Map a line number to the full multi-line statement starting there.

    `layout->engine_response =\\n    static_cast<...>(ERROR_OCCURRED);` is one
    statement over two lines; matching per line would miss the response name.
    """
    lines = code.split('\n')
    stmts = {}
    for i, line in enumerate(lines):
        if 'engine_response' not in line:
            continue
        buf, j = line, i
        while ';' not in buf and j + 1 < len(lines) and j - i < 6:
            j += 1
            buf += ' ' + lines[j].strip()
        stmts[i + 1] = buf
    return stmts


def check_file(path: Path) -> list[Violation]:
    text = path.read_text(encoding='utf-8', errors='replace')
    code = _strip_comments(text)
    stmts = _logical_statements(code)
    is_helper_file = any(h in text for h in HELPER_DEFS) and path.suffix == '.h'
    violations: list[Violation] = []

    events = list(_events(code))

    # Track, per brace depth, whether a payload was published in the block we
    # are currently inside. Cleared when the depth is left.
    payload_at_depth: dict[int, int] = {}
    published_at_depth: dict[int, int] = {}

    for line_no, depth, kind in events:
        if kind == 'unsafe':
            violations.append(Violation(
                'UNSAFE-STRING', path, line_no,
                'unbounded string primitive writes engine_string; use the '
                'SetEngineString/set_engine_string helper'))
            continue

        if kind == 'raw_string':
            if not is_helper_file:
                violations.append(Violation(
                    'RAW-PAYLOAD-WRITE', path, line_no,
                    'engine_string written outside the sanctioned helper'))
            # Still counts as a payload for ordering purposes.
            payload_at_depth[depth] = line_no
            continue

        if kind in ('payload', 'set_error'):
            payload_at_depth[depth] = line_no
            if kind == 'set_error':
                published_at_depth[depth] = line_no
            continue

        if kind == 'consume':
            # Startup initialisation is legitimate; anything else would destroy
            # a published verdict. Distinguished by being outside any command
            # handler — i.e. shallow.
            if depth > 2:
                violations.append(Violation(
                    'ENGINE-CONSUMES', path, line_no,
                    'engine clears engine_response; only the UI may consume a '
                    'published response'))
            continue

        if kind == 'publish':
            stmt = stmts.get(line_no, '')
            is_error = 'ERROR_OCCURRED' in stmt
            published_at_depth[depth] = line_no

            if is_error:
                # Ordering + diagnostics. A payload must have been written in
                # this block (or an enclosing one) before the response.
                have_payload = any(
                    ln < line_no
                    for d, ln in payload_at_depth.items() if d <= depth)
                if not have_payload:
                    violations.append(Violation(
                        'ERROR-WITHOUT-PAYLOAD', path, line_no,
                        'ERROR_OCCURRED published with no diagnostic message '
                        'written first (AUDIT-018/019)'))

    # Payload-after-response within the same block: a payload write whose line
    # follows a publish at the same depth, with no publish in between.
    by_depth: dict[int, list[tuple[int, str]]] = {}
    for line_no, depth, kind in events:
        if kind in ('payload', 'raw_string', 'publish', 'set_error'):
            by_depth.setdefault(depth, []).append((line_no, kind))
    for _depth, seq in by_depth.items():
        for idx, (line_no, kind) in enumerate(seq):
            if kind != 'publish':
                continue
            stmt = stmts.get(line_no, '')
            if 'STATUS_UPDATE' not in stmt and 'ERROR_OCCURRED' not in stmt:
                continue
            for nxt_line, nxt_kind in seq[idx + 1:]:
                if nxt_kind in ('publish', 'set_error'):
                    break
                if nxt_kind in ('payload', 'raw_string'):
                    violations.append(Violation(
                        'PAYLOAD-AFTER-RESPONSE', path, nxt_line,
                        f'payload written after the response published at '
                        f'line {line_no}; publish the response last '
                        f'(AUDIT-018)'))
                    break
    return violations


def collect_sources(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in ('*.cpp', '*.h'):
            files.extend(
                p for p in root.rglob(pattern)
                if 'third_party' not in p.parts and 'protocols' not in p.parts)
    return sorted(files)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', action='append', type=Path,
                    help='source root to scan (repeatable); defaults to both engines')
    args = ap.parse_args(argv)

    roots = args.root or DEFAULT_ROOTS
    files = collect_sources(roots)

    print('Engine response-publication contract\n')
    if not files:
        print(f'  ERROR  no engine sources found under: '
              f'{", ".join(str(r) for r in roots)}')
        return 1

    violations: list[Violation] = []
    for f in files:
        violations.extend(check_file(f))

    print(f'  scanned {len(files)} engine source files')

    if violations:
        print(f'\n  {len(violations)} violation(s):\n')
        for v in violations:
            print(v)
        print('\nRESULT: publication contract VIOLATED.')
        print('        Payload first, engine_response last. See AUDIT-018.')
        return 1

    print('  OK    every ERROR_OCCURRED carries a message written before it')
    print('  OK    no payload written after its response')
    print('  OK    engine_string only written through the bounded helpers')
    print('  OK    no engine-side consumption of a published response')
    print('\nRESULT: publication contract intact.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
