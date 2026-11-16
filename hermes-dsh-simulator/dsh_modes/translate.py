# -*- coding: utf-8 -*-
"""The translation layer: DSH-shaped tool calls in, Hermes tools under the hood,
DSH-shaped results out.

Design contract (from the user):
* the model sees 1:1 DSH schemas (see :mod:`constants`);
* every call is *silently forwarded* onto the corresponding Hermes tool(s):
  ``bash`` -> Hermes ``terminal``; ``str_replace_editor`` -> Hermes
  ``read_file`` / ``write_file`` / ``patch`` (the closest Hermes equivalents;
  there is no single ``edit_file`` tool in Hermes);
* the model-facing result text mirrors DSH's renderers byte-for-byte where DSH
  defines one (view/create/str_replace/insert success & error strings; bash's
  ``[Command finished with exit code N]`` trailer).

Local read-only pre-checks (existence, uniqueness of ``old_str``, insert-line
range) are computed from the content the host ``read_file`` already returned —
exactly what DSH does against its fs seam — so DSH's error texts can be
reproduced with the real line numbers before anything is mutated, while every
byte still crosses the host's read tracking, path policy and guards (no local
``open()``). All mutations still go through the Hermes tools so Hermes' own
guards, locks and approvals stay in force.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable

from . import constants as C


@dataclass
class Forwarders:
    """Bound Hermes handlers. Each takes ``(args, kw)`` and returns the raw
    handler result (a JSON string for every tool we forward to)."""

    terminal: Callable[[dict, dict], str]
    read_file: Callable[[dict, dict], str]
    write_file: Callable[[dict, dict], str]
    patch: Callable[[dict, dict], str]


def _parse(raw):
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _strip_trailing_newlines(text: str) -> str:
    return re.sub(r"(?:\r?\n)+$", "", text)


def _append_marker(content: str, marker):
    if marker is None:
        return content
    return marker if content == "" else content + "\n" + marker


def _maybe_truncate(text: str) -> str:
    limit = C.active_max_output_chars()
    if len(text) <= limit:
        return text
    return text[:limit] + C.TRUNCATED_MESSAGE


# -- host reads --------------------------------------------------------------
# No local ``open()`` anywhere below: the editor's content always comes from the
# host ``read_file`` tool, so its read tracking, path policy, binary/encoding
# checks and redaction stay in force exactly as for a native Hermes call.

_READ_PAGE_LIMIT = 2000  # Hermes DEFAULT_READ_LIMIT
_MAX_READ_PAGES = 200    # defensive: 200 x 2000 lines before we call a read inexact
_GUTTER_RE = re.compile(r"^\d+\|")      # host pages render "<n>|content"
_CLAMP_MARKER = "... [truncated]"       # host clamps an over-long line with this
_UNKNOWN_BINARY_RE = re.compile(r"binary file \(unknown binary", re.IGNORECASE)
_MOJIBAKE = "\ufffd"
_page_cycle = [0]       # rotates the internal page size (see _next_page_limit)

_NOT_UTF8_VIEW = 'cannot view "%s": file is not valid UTF-8 text (maybe GBK/ANSI); convert it first'
_NOT_UTF8_EDIT = 'cannot edit "%s": file is not valid UTF-8 text (maybe GBK/ANSI); convert it first'
_NOT_UTF8_INSERT = 'cannot insert into "%s": file is not valid UTF-8 text (maybe GBK/ANSI); convert it first'
_NO_INSERT_READ = (
    'cannot insert into "%s": the host read could not be reproduced verbatim '
    "(an over-long line was clamped or content was redacted); use str_replace instead"
)


def _page_lines(content: str) -> list:
    """Strip the host's ``<n>|`` gutter back to raw source lines."""
    if content == "":
        return []
    return [_GUTTER_RE.sub("", line, count=1) for line in content.split("\n")]


def _next_page_limit() -> int:
    """A slightly different page size per internal read.

    The host keys both its "unchanged" dedup and its repeated-read block on
    ``(path, offset, limit)``. An edit's own pre-check read is not the model
    re-reading on purpose, so it must not spend the model's read budget on a file
    it is actively editing — a rotating size keeps those keys distinct.
    """
    _page_cycle[0] = (_page_cycle[0] + 1) % 4
    return _READ_PAGE_LIMIT - _page_cycle[0]


def _fit_original(text: str, file_size):
    """Re-fit host page text to the on-disk byte shape. ``(text, matched)``.

    The host read normalises before numbering lines (CR dropped, UTF-8 BOM
    dropped, one final newline dropped), so a faithful ``before`` puts them back:
    enumerate ``(eol, final newline, BOM)`` combinations and keep the one whose
    UTF-8 length equals the host-reported ``file_size``. A BOM weighs exactly as
    much as two CRs plus a newline, so a short file can admit two shapes — that
    is reported as *unmatched* rather than guessed: callers that rewrite the file
    must never invent bytes.
    """
    if not isinstance(file_size, int) or file_size <= 0:
        return text, True
    hits = []
    for eol in ("\n", "\r\n"):
        body = text if eol == "\n" else text.replace("\n", eol)
        for tail in ("", eol):
            for prefix in ("", "\ufeff"):
                candidate = prefix + body + tail
                try:
                    size = len(candidate.encode("utf-8"))
                except Exception:
                    continue
                if size == file_size and candidate not in hits:
                    hits.append(candidate)
    if len(hits) == 1:
        return hits[0], True
    return text, False


def _host_read_page(path: str, fwd: Forwarders, kw: dict, offset: int, limit: int):
    """One host read page. ``(page, error)``.

    The host answers a repeated identical ``(path, offset, limit)`` read with an
    "unchanged" stub instead of content (and blocks it after two), so a stub is
    retried with a nudged ``limit``: every caller here needs real bytes.
    """
    for nudge in (0, 1, 2):
        page = _parse(
            fwd.read_file(
                {"path": path, "offset": offset, "limit": max(1, limit - nudge)},
                dict(kw),
            )
        )
        if not isinstance(page, dict):
            return None, 'cannot read "%s": the host read tool returned no page' % path
        if page.get("error"):
            return None, str(page["error"])
        if page.get("dedup") or page.get("content_returned") is False:
            continue
        if isinstance(page.get("content"), str):
            return page, None
        return None, 'cannot read "%s": the host read tool returned no content' % path
    return None, 'cannot read "%s": the host read tool kept returning its dedup stub' % path


def _host_read_all(path: str, fwd: Forwarders, kw: dict, internal: bool = False):
    """Read *path* wholly through the host read tool. ``(text, exact, error)``.

    *text* is the file re-fitted to its on-disk shape (empty on error), *exact*
    is False when the pages could not be reproduced verbatim (a line clamped by
    the host's per-line limit, a mid-line char-budget cut, redacted content, or
    the defensive page cap), and *error* is a host error string. *internal* marks
    a read the plugin needs for its own pre-check (edit/insert) rather than one
    the model asked for; it uses a rotating page size so it never trips the
    host's repeated-read guard.
    """
    lines = []
    offset = 1
    page_limit = _next_page_limit() if internal else _READ_PAGE_LIMIT
    total_lines = None
    file_size = None
    lossy = False
    finished = False
    for _ in range(_MAX_READ_PAGES):
        page, error = _host_read_page(path, fwd, kw, offset, page_limit)
        if error:
            return "", False, error
        if not isinstance(page, dict):
            return "", False, 'cannot read "%s": the host read tool returned no page' % path
        if total_lines is None:
            total_lines = page.get("total_lines")
            file_size = page.get("file_size")
        page_lines = _page_lines(page["content"])
        if page.get("truncated_lines"):
            lossy = True
        if any(line.endswith(_CLAMP_MARKER) for line in page_lines):
            lossy = True
        lines.extend(page_lines)
        try:
            next_offset = int(page.get("next_offset") or 0)
        except (TypeError, ValueError):
            next_offset = 0
        if next_offset <= offset:
            finished = True
            break
        offset = next_offset
    if not finished:
        lossy = True  # page cap: the tail never arrived
    if isinstance(total_lines, int) and total_lines != len(lines):
        lossy = True
    text, matched = _fit_original("\n".join(lines), file_size)
    return text, matched and not lossy, None


def _host_error_is_encoding(error) -> bool:
    """True when the host refused the read as undecodable bytes (a legacy-encoded
    or genuine binary file) rather than as a path/guard/permission problem."""
    text = str(error or "")
    return bool(_UNKNOWN_BINARY_RE.search(text)) or "not valid utf-8" in text.lower()


def _text_is_undecodable(text: str, exact: bool) -> bool:
    """U+FFFD *and* no byte-size fit: the file is not UTF-8.

    A valid UTF-8 file may legitimately contain U+FFFD (3 bytes, so the fit still
    matches); replacements from decoding a GBK/ANSI file never do.
    """
    return bool(text) and _MOJIBAKE in text and not exact


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

def handle_bash(args: dict, kw: dict, fwd: Forwarders) -> str:
    command = args.get("command")
    if not isinstance(command, str) or command.strip() == "":
        # Not a DSH string (its schema rejects this earlier); short and clear.
        return "Parameter `command` is required for tool: bash"

    raw = fwd.terminal({"command": command, "timeout": C.BASH_TIMEOUT_S}, dict(kw))
    body = _parse(raw)
    if not isinstance(body, dict):
        return raw if isinstance(raw, str) else str(raw)

    out = _strip_trailing_newlines(body.get("output") or "")
    err = body.get("error")
    code = body.get("exit_code")

    yielded = body.get("yielded_session_id")
    if yielded:
        note = "[Command moved to the background: session_id %s]" % yielded
        return _append_marker(out, note)

    if err:
        low = str(err).lower()
        if "timed out" in low:
            match = re.search(r"timed out after (\d+)", str(err))
            secs = int(match.group(1)) if match else C.BASH_TIMEOUT_S
            partial = _append_marker(out, C.TIMEOUT_STATUS_MARKER)
            return "\n".join([C.timeout_intro(secs), partial, C.SHELL_RESET_MESSAGE])
        text = str(err)
        if isinstance(code, int) and not isinstance(code, bool) and code >= 0:
            text = _append_marker(text, C.finished_marker(code))
        return text

    if isinstance(code, int) and not isinstance(code, bool):
        if code >= 0:
            return _append_marker(out, C.finished_marker(code))
        return out
    return out


# ---------------------------------------------------------------------------
# str_replace_editor
# ---------------------------------------------------------------------------

_EDITOR_COMMANDS = ("view", "create", "str_replace", "insert")


def handle_editor(args: dict, kw: dict, fwd: Forwarders) -> str:
    command = args.get("command")
    path = args.get("path")
    if command not in _EDITOR_COMMANDS:
        return (
            "Unknown command: %s. Allowed options are: `view`, `create`, `str_replace`, `insert`."
            % (command,)
        )
    if not isinstance(path, str) or path.strip() == "":
        return C.ERR_PATH_EMPTY
    if not os.path.isabs(path):
        return C.err_not_absolute(path)
    try:
        if command == "view":
            return _editor_view(args, path, fwd, kw)
        if command == "create":
            return _editor_create(args, path, fwd, kw)
        if command == "str_replace":
            return _editor_replace(args, path, fwd, kw)
        return _editor_insert(args, path, fwd, kw)
    except Exception as exc:  # unexpected — surface it rather than crashing the turn
        return "Error: %s" % exc


def _join(parent: str, name: str) -> str:
    if "\\" in parent and "/" not in parent:
        return parent.rstrip("\\") + "\\" + name
    return parent.rstrip("/") + "/" + name


# -- view -------------------------------------------------------------------

def _editor_view(args: dict, path: str, fwd: Forwarders, kw: dict) -> str:
    if not os.path.exists(path):
        return C.err_not_exist(path)
    if os.path.isdir(path):
        view_range = args.get("view_range")
        if view_range is not None:
            return "The `view_range` parameter is not allowed when `path` points to a directory."
        return _dir_listing(path)
    if not os.path.isfile(path):
        return 'cannot view "%s": not a regular file or directory' % path

    content, exact, error = _host_read_all(path, fwd, kw)
    if error:
        if _host_error_is_encoding(error):
            return _NOT_UTF8_VIEW % path
        return error
    if _text_is_undecodable(content, exact):
        return _NOT_UTF8_VIEW % path
    return _format_view(path, content, args.get("view_range"))


def _format_view(path: str, content: str, view_range) -> str:
    all_lines = content.split("\n")
    lines = all_lines
    initial_line = 1
    header_range = None
    if view_range is not None:
        shaped = (
            isinstance(view_range, (list, tuple))
            and len(view_range) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in view_range)
        )
        if not shaped:
            return C.err_view_range_shape(view_range)
        initial_line, final_line = view_range
        if initial_line < 1 or initial_line > len(all_lines):
            return C.err_view_range_start(view_range, initial_line, len(all_lines))
        if final_line > len(all_lines):
            return C.err_view_range_end(view_range, final_line, len(all_lines))
        if final_line != -1 and final_line < initial_line:
            return C.err_view_range_order(view_range, final_line, initial_line)
        if final_line == -1:
            lines = all_lines[initial_line - 1:]
        else:
            lines = all_lines[initial_line - 1:final_line]
        header_range = (initial_line, final_line)
    numbered = "\n".join(
        "%s  %s" % (str(initial_line + index).rjust(6), line) for index, line in enumerate(lines)
    )
    text = C.view_header(path, len(all_lines), header_range) + ":\n" + numbered + "\n"
    return _maybe_truncate(text)


def _dir_listing(path: str) -> str:
    skip = ("node_modules", "__pycache__")
    rows = ["d\t" + path]

    def visit(dirpath: str, depth: int) -> None:
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            return
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in skip:
                continue
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:
                is_dir = is_file = False
            kind = "d" if is_dir else ("f" if is_file else "?")
            child = _join(dirpath, name)
            rows.append("%s\t%s" % (kind, child))
            if is_dir and depth < 2:
                visit(child, depth + 1)

    visit(path, 1)
    rows.sort(key=lambda row: row.split("\t", 1)[1])
    listing = _maybe_truncate("\n".join(rows) + "\n")
    return C.dir_header(path) + "\n" + listing + "\n"


# -- create -----------------------------------------------------------------

def _editor_create(args: dict, path: str, fwd: Forwarders, kw: dict) -> str:
    file_text = args.get("file_text")
    if file_text is None:
        return C.err_required("file_text", "create")
    if not isinstance(file_text, str):
        return C.err_required("file_text", "create")
    if os.path.exists(path):
        return C.err_create_exists(path)
    result = _parse(fwd.write_file({"path": path, "content": file_text}, dict(kw)))
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return C.ok_created(path)


# -- str_replace ------------------------------------------------------------

def _match_offsets(content: str, search: str):
    offsets = []
    start = 0
    while True:
        found = content.find(search, start)
        if found < 0:
            return offsets
        offsets.append(found)
        start = found + len(search)


def _line_numbers(content: str, offsets):
    return [content.count("\n", 0, offset) + 1 for offset in offsets]


def _editor_replace(args: dict, path: str, fwd: Forwarders, kw: dict) -> str:
    # DSH checks the explicit-null placeholder for new_str before anything else.
    if "new_str" in args and args.get("new_str") is None:
        return C.ERR_NEW_STR_NULL
    old_value = args.get("old_str")
    if old_value is None:
        return C.err_required("old_str", "str_replace")
    if not isinstance(old_value, str):
        return C.err_required("old_str", "str_replace")
    if old_value == "":
        return C.err_empty("old_str", "str_replace")
    if "new_str" in args:
        new_value = args.get("new_str")
    else:
        new_value = ""  # omission requests deletion
    if not isinstance(new_value, str):
        return C.err_required("new_str", "str_replace")

    if not os.path.exists(path):
        return C.err_not_exist(path)
    if os.path.isdir(path):
        return C.err_is_dir(path)
    if not os.path.isfile(path):
        return C.err_edit_not_file(path)

    before, exact, error = _host_read_all(path, fwd, kw, internal=True)
    if error:
        if _host_error_is_encoding(error):
            return _NOT_UTF8_EDIT % path
        return error
    if _text_is_undecodable(before, exact):
        return _NOT_UTF8_EDIT % path

    if exact:
        offsets = _match_offsets(before, old_value)
        if not offsets:
            return C.err_edit_not_found(path, old_value)
        if len(offsets) > 1:
            return C.err_edit_multiple(path, old_value, _line_numbers(before, offsets))
    # Inexact page (clamped line / redacted content): skip the local pre-check
    # instead of denying an edit DSH would have found — the host patch decides.

    result = _parse(
        fwd.patch(
            {
                "mode": "replace",
                "path": path,
                "old_string": old_value,
                "new_string": new_value,
                "replace_all": False,
            },
            dict(kw),
        )
    )
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return C.ok_edited(path)


# -- insert -------------------------------------------------------------------

def _insert_by_anchor(path: str, before: str, insert_line: int, new_value: str, fwd: Forwarders, kw: dict):
    """Insert one line without rewriting the file, through the host patch.

    Only used when the host page cannot be reproduced byte-for-byte (a short
    file where a BOM and two CRs weigh the same, or a clamped line): the anchor
    line's TEXT is still exact, and a single-line anchor carries no line ending,
    so splicing it cannot disturb the rest of the file. Returns the result text,
    or None when no anchor is unambiguous.
    """
    if not isinstance(insert_line, int) or isinstance(insert_line, bool):
        return None
    lines = before.split("\n")
    if insert_line < 0 or insert_line > len(lines):
        return None
    candidates = []
    if insert_line > 0:
        candidates.append((insert_line - 1, False))   # append below the line above
    if insert_line < len(lines):
        candidates.append((insert_line, True))        # prepend above the line below
    for index, prepend in candidates:
        anchor = lines[index]
        if not anchor.strip() or before.count(anchor) != 1:
            continue  # blank or non-unique anchor: the host patch could misfire
        replacement = (new_value + "\n" + anchor) if prepend else (anchor + "\n" + new_value)
        result = _parse(
            fwd.patch(
                {
                    "mode": "replace",
                    "path": path,
                    "old_string": anchor,
                    "new_string": replacement,
                    "replace_all": False,
                },
                dict(kw),
            )
        )
        if isinstance(result, dict) and result.get("error"):
            return None
        return C.ok_edited(path)
    return None


def _editor_insert(args: dict, path: str, fwd: Forwarders, kw: dict) -> str:
    insert_line = args.get("insert_line")
    if insert_line is None:
        return C.err_required("insert_line", "insert")
    new_value = args.get("new_str")
    if new_value is None:
        return C.err_required("new_str", "insert")
    if not isinstance(new_value, str):
        return C.err_required("new_str", "insert")

    if not os.path.exists(path):
        return C.err_not_exist(path)
    if os.path.isdir(path):
        return C.err_is_dir(path)
    if not os.path.isfile(path):
        return C.err_insert_not_file(path)

    # insert rewrites the whole file, so a page that is not byte-faithful would
    # silently damage it: splice through the host patch when the byte shape is
    # ambiguous, and refuse when even that anchor is unusable.
    # The full paged read also establishes the host's write_file baseline.
    before, exact, error = _host_read_all(path, fwd, kw, internal=True)
    if error:
        if _host_error_is_encoding(error):
            return _NOT_UTF8_INSERT % path
        return error
    if _text_is_undecodable(before, exact):
        return _NOT_UTF8_INSERT % path
    if not exact:
        spliced = _insert_by_anchor(path, before, insert_line, new_value, fwd, kw)
        return spliced if spliced is not None else (_NO_INSERT_READ % path)

    lines = before.split("\n")
    if (
        not isinstance(insert_line, int)
        or isinstance(insert_line, bool)
        or insert_line < 0
        or insert_line > len(lines)
    ):
        return C.err_insert_range(insert_line, len(lines))

    after = "\n".join(lines[:insert_line] + new_value.split("\n") + lines[insert_line:])
    result = _parse(fwd.write_file({"path": path, "content": after}, dict(kw)))
    if isinstance(result, dict) and result.get("stale_write_blocked"):
        # The host refuses a whole-file overwrite when a read could not record a
        # stable stat snapshot (on Windows a cached directory-entry ctime can
        # differ from the file's own). Every byte was read, so splice the line in
        # through the host patch instead of dropping the insert.
        spliced = _insert_by_anchor(path, before, insert_line, new_value, fwd, kw)
        if spliced is not None:
            return spliced
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return C.ok_edited(path)