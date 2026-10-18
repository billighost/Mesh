"""
mesh/ui.py — Terminal design system for Mesh.

One small, dependency-free module that every command uses to talk to the human
at the keyboard, so the whole CLI looks and feels the same.

Design rules
  - stdlib only and import-cheap (keeps `mesh --help` instant)
  - results go to stdout, status/diagnostics/errors go to stderr (pipes stay clean)
  - colour is a bonus, never required: NO_COLOR, FORCE_COLOR, TERM=dumb, non-TTY
    and legacy 16-colour terminals are all handled
  - Unicode is a bonus, never required: every symbol has an ASCII fallback
    (force with MESH_ASCII=1)
  - never print raw tags like [OK] / [..] / [INPUT]; use one consistent
    vocabulary of symbols and words instead
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, NoReturn, Optional, Sequence, Union

__all__ = [
    "style", "color_enabled", "unicode_enabled", "interactive", "term_width",
    "sym", "visible_len", "truncate", "wrap", "plural", "relative_time",
    "short_path", "meter", "badge",
    "blank", "out", "err", "header", "section", "rule", "kv", "table", "box",
    "success", "info", "warn", "error", "fail", "step", "hint", "note", "next_steps",
    "readline_safe",
    "spinner", "confirm", "prompt_text", "json_out", "command_hint",
]

# ─────────────────────────────────────────────────────────────────────────────
# Capability detection
# ─────────────────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VT_ENABLED = False
_NI_HINT_SHOWN = False
_OUT_LOCK = threading.RLock()
_ACTIVE_SPINNERS: list["_Spinner"] = []


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _enable_windows_vt() -> None:
    """Turn on ANSI escape processing for legacy Windows consoles (best effort, once)."""
    global _VT_ENABLED
    if _VT_ENABLED or os.name != "nt":
        return
    _VT_ENABLED = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for std_handle in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def color_enabled(stream: Any = None) -> bool:
    """Colour policy: NO_COLOR wins, then FORCE_COLOR, then 'is this a real terminal?'."""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR") or _flag("MESH_NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or _flag("MESH_FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    ok = hasattr(stream, "isatty") and stream.isatty()
    if ok:
        _enable_windows_vt()
    return ok


def _color_depth() -> int:
    """24 = truecolor, 8 = 256 colours, 4 = basic 16 colours."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return 24
    if os.name == "nt" or os.environ.get("WT_SESSION"):
        return 24
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode", "WezTerm", "ghostty"):
        return 24
    term = os.environ.get("TERM", "").lower()
    if "256" in term or "kitty" in term or "alacritty" in term:
        return 8
    return 4


def unicode_enabled(stream: Any = None) -> bool:
    if _flag("MESH_ASCII"):
        return False
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        "✓─│╭·→█░▲●".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def interactive() -> bool:
    """True when a human is plausibly watching (both stdin and stdout are terminals)."""
    return bool(
        hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    )


def term_width(maximum: int = 110, minimum: int = 48) -> int:
    cols = shutil.get_terminal_size((88, 24)).columns
    return max(minimum, min(cols, maximum))


# ─────────────────────────────────────────────────────────────────────────────
# Styling
# ─────────────────────────────────────────────────────────────────────────────

# name -> ((r, g, b), basic-16 SGR foreground code)
_PALETTE: dict[str, tuple[tuple[int, int, int], int]] = {
    "brand":  ((16, 185, 129), 32),    # emerald
    "info":   ((96, 165, 250), 34),    # blue
    "accent": ((167, 139, 250), 35),   # violet
    "warn":   ((245, 158, 11), 33),    # amber
    "error":  ((248, 113, 113), 31),   # red
    "muted":  ((139, 149, 167), 90),   # grey
}
_ATTRS = {"bold": "1", "dim": "2", "italic": "3", "underline": "4"}


def _rgb_to_256(r: int, g: int, b: int) -> int:
    def to6(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else min(5, (v - 35) // 40)

    return 16 + 36 * to6(r) + 6 * to6(g) + to6(b)


def _sgr(names: Sequence[str]) -> str:
    depth = _color_depth()
    codes: list[str] = []
    for n in names:
        if n in _ATTRS:
            codes.append(_ATTRS[n])
        elif n in _PALETTE:
            (r, g, b), basic = _PALETTE[n]
            if depth == 24:
                codes.append(f"38;2;{r};{g};{b}")
            elif depth == 8:
                codes.append(f"38;5;{_rgb_to_256(r, g, b)}")
            else:
                codes.append(str(basic))
    return ";".join(codes)


def style(text: Any, *names: str, stream: Any = None) -> str:
    """style("hello", "bold", "brand") — returns plain text when colour is off."""
    s = str(text)
    if not names or not color_enabled(stream):
        return s
    codes = _sgr(names)
    if not codes:
        return s
    return f"\x1b[{codes}m{s}\x1b[0m"


def _estyle(text: Any, *names: str) -> str:
    """style() for text destined to stderr."""
    return style(text, *names, stream=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Symbols
# ─────────────────────────────────────────────────────────────────────────────

_SYMBOLS_UNI = {
    "ok": "✓", "fail": "✗", "warn": "▲", "info": "●", "pending": "○",
    "arrow": "→", "dot": "·", "bar": "┃", "bullet": "•", "rule": "─",
    "meter_on": "█", "meter_off": "░", "brand": "◆", "prompt": "❯",
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "v": "│", "h": "─",
    "ellipsis": "…", "q": "?",
}
_SYMBOLS_ASCII = {
    "ok": "+", "fail": "x", "warn": "!", "info": "*", "pending": "o",
    "arrow": "->", "dot": "-", "bar": "|", "bullet": "*", "rule": "-",
    "meter_on": "#", "meter_off": "-", "brand": "*", "prompt": ">",
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "v": "|", "h": "-",
    "ellipsis": "...", "q": "?",
}


def sym(name: str, stream: Any = None) -> str:
    table = _SYMBOLS_UNI if unicode_enabled(stream) else _SYMBOLS_ASCII
    return table.get(name, "")


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────


def visible_len(text: str) -> int:
    """Display width of `text` ignoring ANSI codes and counting wide (CJK) glyphs as 2."""
    plain = _ANSI_RE.sub("", text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate(plain: str, width: int) -> str:
    """Truncate *plain* (un-styled) text to `width` columns with an ellipsis."""
    plain = " ".join(str(plain).split())
    if visible_len(plain) <= width:
        return plain
    ell = sym("ellipsis")
    budget = max(1, width - visible_len(ell))
    out_chars: list[str] = []
    used = 0
    for ch in plain:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > budget:
            break
        out_chars.append(ch)
        used += w
    return "".join(out_chars).rstrip() + ell


def wrap(text: str, width: int, indent: str = "", first_indent: Optional[str] = None, max_lines: Optional[int] = None) -> list[str]:
    """Word-wrap plain text (collapsing whitespace). Optionally cap at `max_lines` with an ellipsis."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return [first_indent if first_indent is not None else indent]
    lines = textwrap.wrap(
        flat,
        width=max(10, width),
        initial_indent=first_indent if first_indent is not None else indent,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    )
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = truncate(lines[-1], max(10, width - 1)).rstrip(sym("ellipsis")) + sym("ellipsis")
    return lines


def plural(n: int, singular: str, plural_form: Optional[str] = None) -> str:
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw or raw.lower() in ("never", "error", "none"):
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(value: Any, now: Optional[datetime] = None) -> str:
    """'just now', '5m ago', '3h ago', '2d ago', '3w ago', or 'Mar 4, 2025'."""
    dt = _parse_dt(value)
    if dt is None:
        return "never" if str(value or "").strip().lower() == "never" else ""
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 45:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60) or 1}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 14:
        return f"{int(secs // 86400)}d ago"
    if secs < 86400 * 60:
        return f"{int(secs // (86400 * 7))}w ago"
    return dt.strftime("%b %d, %Y").replace(" 0", " ")


def short_path(path: Any) -> str:
    """Replace the home directory with ~ for compact display."""
    p = str(path)
    try:
        home = str(Path.home())
        if p == home:
            return "~"
        if p.startswith(home + os.sep):
            return "~" + p[len(home):]
    except Exception:
        pass
    return p


def meter(fraction: float, width: int = 8, stream: Any = None) -> str:
    """Compact bar like ████░░░░ coloured by strength (green / amber / grey)."""
    f = max(0.0, min(1.0, float(fraction or 0.0)))
    filled = int(round(f * width))
    color = "brand" if f >= 0.7 else ("warn" if f >= 0.4 else "muted")
    on, off = sym("meter_on"), sym("meter_off")
    return style(on * filled, color, stream=stream) + style(off * (width - filled), "muted", "dim", stream=stream)


def badge(text: str, color: str = "muted") -> str:
    """A small status word: coloured when possible, [bracketed] when not."""
    if color_enabled():
        return style(text, color, "bold")
    return f"[{text}]"


def command_hint(cmd: str) -> str:
    """Render a shell command so it stands out inside a sentence."""
    return style(cmd, "info") if color_enabled() else f"`{cmd}`"


# ─────────────────────────────────────────────────────────────────────────────
# Output primitives
# ─────────────────────────────────────────────────────────────────────────────

IND = "  "


def out(text: str = "") -> None:
    print(text)


def err(text: str = "") -> None:
    print(text, file=sys.stderr)


def blank() -> None:
    print()


def rule(width: Optional[int] = None, label: Optional[str] = None) -> str:
    width = width or min(term_width(), 64)
    ch = sym("rule")
    if label:
        lead = f"{ch}{ch} {label} "
        return style(lead + ch * max(2, width - visible_len(lead)), "muted", "dim")
    return style(ch * width, "muted", "dim")


def header(title: str, *meta: str) -> None:
    """
      ◆ Title   meta · meta
      ──────────────────────
    """
    print()
    meta_s = ""
    metas = [m for m in meta if m]
    if metas:
        meta_s = "  " + style(f" {sym('dot')} ".join(metas), "muted")
    print(f"{IND}{style(sym('brand'), 'brand')} {style(title, 'bold')}{meta_s}")
    print(f"{IND}{rule(min(term_width() - 4, 64))}")


def section(title: str) -> None:
    print()
    print(f"{IND}{style(title.upper(), 'muted', 'bold')}")


def kv(rows: Iterable[tuple[str, Any]], key_width: Optional[int] = None, indent: str = IND) -> None:
    """Aligned 'key   value' rows. Rows whose value is None are skipped."""
    items = [(k, v) for k, v in rows if v is not None]
    if not items:
        return
    kw = key_width or max(len(k) for k, _ in items) + 2
    for k, v in items:
        print(f"{indent}{style(k.ljust(kw), 'muted')}{v}")


Cell = Union[str, tuple]


def _cell_parts(cell: Cell) -> tuple[str, tuple[str, ...]]:
    if isinstance(cell, tuple):
        return str(cell[0]), tuple(cell[1:])
    return str(cell), ()


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Cell]],
    *,
    aligns: Optional[Sequence[str]] = None,
    flex: Optional[int] = None,
    indent: str = IND,
    gap: int = 2,
) -> None:
    """
    Borderless table. Cells are plain strings or (text, style, style...) tuples.
    Column `flex` absorbs/shrinks to keep the table inside the terminal.
    Cells passed as plain str that already contain ANSI codes are never truncated.
    """
    ncols = len(headers)
    aligns = list(aligns or ["l"] * ncols)
    parts = [[_cell_parts(c) for c in row] for row in rows]
    widths = [visible_len(h) for h in headers]
    for row in parts:
        for i, (text, _) in enumerate(row):
            widths[i] = max(widths[i], visible_len(text))

    if flex is not None:
        avail = term_width() - len(indent) - gap * (ncols - 1)
        other = sum(w for i, w in enumerate(widths) if i != flex)
        widths[flex] = max(12, min(widths[flex], avail - other))

    def render(text: str, styles: tuple[str, ...], i: int) -> str:
        plain_has_ansi = bool(_ANSI_RE.search(text))
        shown = text if plain_has_ansi else truncate(text, widths[i])
        pad = widths[i] - visible_len(shown)
        body = style(shown, *styles) if styles else shown
        if i == ncols - 1 and aligns[i] != "r":
            return body
        if aligns[i] == "r":
            return " " * max(0, pad) + body
        return body + " " * max(0, pad)

    head_cells = []
    for i, h in enumerate(headers):
        last_left = i == ncols - 1 and aligns[i] != "r"
        txt = h.upper() if last_left else _pad_plain(h.upper(), widths[i], aligns[i])
        head_cells.append(style(txt, "muted", "bold"))
    print(f"{indent}{(' ' * gap).join(head_cells).rstrip()}")
    print(f"{indent}{style(sym('rule') * min(sum(widths) + gap * (ncols - 1), term_width() - len(indent)), 'muted', 'dim')}")
    for row in parts:
        cells = [render(text, styles, i) for i, (text, styles) in enumerate(row)]
        print(indent + (" " * gap).join(cells).rstrip())


def _pad_plain(text: str, width: int, align: str) -> str:
    pad = max(0, width - visible_len(text))
    return " " * pad + text if align == "r" else text + " " * pad


def box(lines: Sequence[str], title: Optional[str] = None, color: str = "brand", indent: str = IND) -> None:
    """Rounded panel. `lines` may contain ANSI styling."""
    inner = max([visible_len(l) for l in lines] + [visible_len(title or "") + 2, 20])
    inner = min(inner, term_width() - len(indent) - 4)
    tl, tr, bl, br, v, h = (sym(n) for n in ("tl", "tr", "bl", "br", "v", "h"))
    if title:
        top = style(tl + h + " ", color) + style(title, "bold", color) + style(" " + h * max(1, inner - visible_len(title) - 1) + tr, color)
    else:
        top = style(tl + h * (inner + 2) + tr, color)
    print(f"{indent}{top}")
    for l in lines:
        pad = max(0, inner - visible_len(l))
        print(f"{indent}{style(v, color)} {l}{' ' * pad} {style(v, color)}")
    print(f"{indent}{style(bl + h * (inner + 2) + br, color)}")


# ── status lines ──────────────────────────────────────────────────────────────


def _status_line(symbol_name: str, color: str, msg: str, detail: Optional[str], stream: Any) -> None:
    st = (lambda t, *n: style(t, *n, stream=stream))
    glyph = st(sym(symbol_name, stream), color, "bold")
    with _OUT_LOCK:
        _clear_spinner_line()
        print(f"{IND}{glyph} {msg}", file=stream)
        if detail:
            for line in str(detail).splitlines():
                print(f"{IND}  {st(line, 'muted')}", file=stream)


def success(msg: str, detail: Optional[str] = None, *, stream: Any = None) -> None:
    _status_line("ok", "brand", msg, detail, stream or sys.stdout)


def info(msg: str, detail: Optional[str] = None, *, stream: Any = None) -> None:
    _status_line("info", "info", msg, detail, stream or sys.stdout)


def step(msg: str, detail: Optional[str] = None, *, stream: Any = None) -> None:
    s = stream or sys.stdout
    _status_line("pending", "muted", style(msg, "muted", stream=s), detail, s)


def warn(msg: str, hint_text: Optional[str] = None, *, stdout: bool = False) -> None:
    stream = sys.stdout if stdout else sys.stderr
    _status_line("warn", "warn", msg, None, stream)
    if hint_text:
        hint(hint_text, stream=stream)


def error(msg: str, hint_text: Optional[str] = None) -> None:
    _status_line("fail", "error", style(msg, "error", stream=sys.stderr), None, sys.stderr)
    if hint_text:
        hint(hint_text, stream=sys.stderr)


def fail(msg: str, hint_text: Optional[str] = None, code: int = 1) -> NoReturn:
    """Print a styled error to stderr and exit."""
    error(msg, hint_text)
    sys.exit(code)


def note(msg: str) -> None:
    """Quiet one-line diagnostic on stderr (e.g. 'falling back to local database')."""
    with _OUT_LOCK:
        _clear_spinner_line()
        print(f"{IND}{style(msg, 'muted', stream=sys.stderr)}", file=sys.stderr)


def hint(text: str, stream: Any = None) -> None:
    stream = stream or sys.stdout
    arrow = style(sym("arrow", stream), "muted", stream=stream)
    with _OUT_LOCK:
        _clear_spinner_line()
        print(f"{IND}  {arrow} {style(text, 'muted', stream=stream)}", file=stream)


def next_steps(items: Sequence[tuple[str, str]], title: str = "Next") -> None:
    """Footer listing suggested follow-up commands: [(command, description), ...]."""
    if not items:
        return
    print()
    print(f"{IND}{style(title, 'muted', 'bold')}")
    cw = max(len(c) for c, _ in items) + 2
    for cmd, desc in items:
        print(f"{IND}  {style(cmd.ljust(cw), 'info')}{style(desc, 'muted')}")


def json_out(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


# ─────────────────────────────────────────────────────────────────────────────
# Interaction
# ─────────────────────────────────────────────────────────────────────────────


def _readline_safe(prompt: str) -> str:
    """
    Wrap ANSI sequences in \\001/\\002 so GNU readline/libedit computes the prompt width correctly.
    Importing readline also gives the user arrow-key editing and history in prompts.
    When readline isn't available the markers would print literally, so we leave the prompt alone.
    """
    if os.name == "nt":
        return prompt
    try:
        # Imported for its side effect: enables arrow-key editing and history.
        # `__import__` keeps linters from flagging an otherwise "unused" import.
        __import__("readline")
    except ImportError:
        return prompt
    return re.sub(r"(\x1b\[[0-9;]*m)", "\001\\1\002", prompt)


readline_safe = _readline_safe


def prompt_text(label: str, default: Optional[str] = None) -> str:
    """Ask for a line of free text. Raises EOFError/KeyboardInterrupt like input()."""
    suffix = f" {style('(' + default + ')', 'muted')}" if default else ""
    q = style(sym("q"), "info", "bold")
    with _spinners_paused():
        raw = input(_readline_safe(f"{IND}{q} {style(label, 'bold')}{suffix}{style(' ' + sym('arrow') + ' ', 'muted')}")).strip()
    return raw or (default or "")


def confirm(question: str, *, default: bool = False, assume_yes: bool = False, detail: Optional[str] = None) -> bool:
    """
    Styled y/n prompt. Returns the default-False in non-interactive contexts and tells
    the user how to proceed instead of failing silently.
    """
    if assume_yes or _flag("MESH_AUTO_APPROVE"):
        return True
    if _flag("MESH_NON_INTERACTIVE") or not interactive():
        global _NI_HINT_SHOWN
        warn(f"Needs your approval: {' '.join(question.split())}")
        if not _NI_HINT_SHOWN:
            _NI_HINT_SHOWN = True
            hint("This isn't an interactive terminal. Re-run in one, or pass --yes to approve.", stream=sys.stderr)
        return False
    q = style(sym("q"), "info", "bold")
    choices = style("[Y/n]" if default else "[y/N]", "muted")
    with _spinners_paused():
        if detail:
            print(f"{IND}{q} {style(question, 'bold')}")
            print(f"{IND}  {style(detail, 'muted')}")
            prompt = f"{IND}  {choices} "
        else:
            prompt = f"{IND}{q} {style(question, 'bold')} {choices} "
        try:
            ans = input(_readline_safe(prompt)).strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return False
    if not ans:
        return default
    return ans in ("y", "yes")


def _clear_spinner_line() -> None:
    """Erase a spinner frame before anything else prints (call while holding _OUT_LOCK)."""
    if any(s.animating for s in _ACTIVE_SPINNERS):
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()


@contextmanager
def _spinners_paused() -> Iterator[None]:
    """Stop spinner animation while the user is typing an answer, then resume."""
    paused = [s for s in _ACTIVE_SPINNERS if s.running and not s._paused.is_set()]
    with _OUT_LOCK:
        for s in paused:
            s._pause()
    try:
        yield
    finally:
        for s in paused:
            s._resume()


class _Spinner:
    FRAMES_UNI = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    FRAMES_ASCII = "|/-\\"

    def __init__(self, message: str) -> None:
        self.message = message
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._drawn = False
        self._active = (
            hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
            and color_enabled(sys.stderr)
            and not _flag("MESH_NO_SPINNER")
            and os.environ.get("TERM", "").lower() != "dumb"
        )
        self._start = time.monotonic()

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    @property
    def animating(self) -> bool:
        return self._thread is not None and self._drawn and not self._paused.is_set() and not self._stop.is_set()

    def update(self, message: str) -> None:
        self.message = message

    def _pause(self) -> None:
        self._paused.set()
        sys.stderr.write("\r\x1b[K\x1b[?25h")  # clear the frame, show the cursor for typing
        sys.stderr.flush()
        self._drawn = False

    def _resume(self) -> None:
        self._paused.clear()
        with _OUT_LOCK:
            sys.stderr.write("\x1b[?25l")
            sys.stderr.flush()

    def _run(self) -> None:
        frames = self.FRAMES_UNI if unicode_enabled(sys.stderr) else self.FRAMES_ASCII
        i = 0
        while not self._stop.wait(0.08):
            if self._paused.is_set() or time.monotonic() - self._start < 0.25:  # don't flash for fast work
                continue
            with _OUT_LOCK:
                if self._paused.is_set() or self._stop.is_set():
                    continue
                frame = _estyle(frames[i % len(frames)], "brand")
                sys.stderr.write(f"\r{IND}{frame} {_estyle(self.message, 'muted')}\x1b[K")
                sys.stderr.flush()
                self._drawn = True
            i += 1

    def __enter__(self) -> "_Spinner":
        if self._active:
            sys.stderr.write("\x1b[?25l")  # hide cursor
            _ACTIVE_SPINNERS.append(self)
            self._thread = threading.Thread(target=self._run, name="mesh-spinner", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=0.5)
            with _OUT_LOCK:
                if self in _ACTIVE_SPINNERS:
                    _ACTIVE_SPINNERS.remove(self)
                sys.stderr.write("\r\x1b[K\x1b[?25h")  # clear line, show cursor
                sys.stderr.flush()
            self._thread = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


def spinner(message: str) -> _Spinner:
    """`with ui.spinner("Searching…") as sp:` — animates on stderr only for real terminals."""
    return _Spinner(message)
