#!/usr/bin/env python3
"""Check that intentionally silent exception handlers explain why.

Reject bare except and BaseException handlers. Silent broad or narrow
handlers need a comment inside or immediately above the handler. AST
inspection excludes docstrings and fixtures. --root selects a tree;
--baseline sets the allowed existing debt. Exit 1 reports violations.
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = [REPO / 'FTHR_UI']

# : Ratchet for existing undocumented silent handlers. Only decrease this
# : baseline as handlers are documented; new debt must fail CI. Bare except
# : and BaseException are forbidden regardless of the baseline.
BASELINE = 92

#: Bodies that do nothing a reader can observe.
_SILENT_NODES = (ast.Pass, ast.Continue, ast.Break)

#: Catching these hides Ctrl-C and interpreter shutdown.
_BASE_NAMES = {'BaseException'}
_BROAD_NAMES = {'Exception', 'BaseException'}


@dataclass
class Finding:
    rule: str
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        try:
            rel = self.path.relative_to(REPO)
        except ValueError:
            rel = self.path
        return f'  {self.rule:<18} {rel}:{self.line}\n      {self.detail}'


def _comment_lines(source: str) -> set[int]:
    """Line numbers carrying a comment. Tokenize, so '#' inside a string
    literal is not mistaken for one."""
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return lines


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """True when the handler body cannot tell anyone anything.

    A bare `return`/`return False`/`return None` counts: the caller gets a
    falsy value with no way to distinguish "nothing to do" from "it broke".
    A `return` with a real expression does not — that is a computed result.
    """
    body = [s for s in handler.body
            if not (isinstance(s, ast.Expr)
                    and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str))]
    if not body:
        return True
    if len(body) > 1:
        return False
    stmt = body[0]
    if isinstance(stmt, _SILENT_NODES):
        return True
    if isinstance(stmt, ast.Return):
        return stmt.value is None or (
            isinstance(stmt.value, ast.Constant)
            and stmt.value.value in (None, False, True))
    return False


def _exception_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    node = handler.type
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    names = set()
    for p in parts:
        if isinstance(p, ast.Name):
            names.add(p.id)
        elif isinstance(p, ast.Attribute):
            names.add(p.attr)
    return names


def _has_justification(handler: ast.ExceptHandler,
                       comments: set[int]) -> bool:
    """A comment on the `except` line, just above it, or inside the body."""
    start = handler.lineno
    end = max((getattr(s, 'end_lineno', s.lineno) or s.lineno)
              for s in handler.body)
    window = set(range(start - 2, end + 1))
    return bool(window & comments)


def check_file(path: Path) -> list[Finding]:
    source = path.read_text(encoding='utf-8', errors='replace')
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Finding('SYNTAX', path, e.lineno or 0, f'cannot parse: {e.msg}')]

    comments = _comment_lines(source)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = _exception_names(node)

        if node.type is None:
            findings.append(Finding(
                'BARE-EXCEPT', path, node.lineno,
                'bare `except:` also catches KeyboardInterrupt and SystemExit; '
                'name the exceptions actually expected'))
            continue

        if names & _BASE_NAMES:
            findings.append(Finding(
                'BASE-EXCEPTION', path, node.lineno,
                '`except BaseException` swallows Ctrl-C and interpreter '
                'shutdown; catch Exception or narrower'))
            continue

        if not _is_silent(node):
            continue

        if _has_justification(node, comments):
            continue

        if names & _BROAD_NAMES:
            findings.append(Finding(
                'BROAD-SWALLOW', path, node.lineno,
                '`except Exception` discards the error with no log and no '
                'comment; log it, narrow it, or explain the silence'))
        else:
            findings.append(Finding(
                'UNDOCUMENTED-PASS', path, node.lineno,
                f'`except {", ".join(sorted(names))}` silently ignores the '
                f'error; add a comment saying why that is correct'))
    return findings


def collect(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == '.py':
            files.append(root)
        elif root.is_dir():
            files.extend(p for p in root.rglob('*.py')
                         if '__pycache__' not in p.parts
                         and 'venv' not in p.parts)
    return sorted(files)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', action='append', type=Path)
    ap.add_argument('--baseline', type=int, default=BASELINE,
                    help='maximum tolerated undocumented silent handlers. '
                         'AUDIT-007 is ratcheting this down; it must never '
                         'be raised.')
    args = ap.parse_args(argv)

    roots = args.root or DEFAULT_ROOTS
    files = collect(roots)

    print('Exception-handling contract\n')
    if not files:
        print(f'  ERROR  no Python sources found under: '
              f'{", ".join(str(r) for r in roots)}')
        return 1

    findings: list[Finding] = []
    for f in files:
        findings.extend(check_file(f))

    print(f'  scanned {len(files)} source files')

    blocking = [f for f in findings
                if f.rule in ('BARE-EXCEPT', 'BASE-EXCEPTION', 'SYNTAX')]
    ratcheted = [f for f in findings if f not in blocking]

    for f in blocking:
        print(f)
    if blocking:
        print(f'\nRESULT: {len(blocking)} forbidden handler(s). '
              f'These are never acceptable.')
        return 1

    print('  OK    no bare `except:`')
    print('  OK    no `except BaseException`')

    if len(ratcheted) > args.baseline:
        print(f'\n  {len(ratcheted)} undocumented silent handler(s), '
              f'baseline is {args.baseline}:\n')
        for f in ratcheted[:40]:
            print(f)
        if len(ratcheted) > 40:
            print(f'  ... and {len(ratcheted) - 40} more')
        print('\nRESULT: silent-handler count ROSE above the baseline.')
        print('        Log it, narrow it, or comment why the silence is right.')
        return 1

    print(f'  OK    {len(ratcheted)} documented/known silent handler(s), '
          f'baseline {args.baseline}')
    print('\nRESULT: exception-handling contract intact.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
