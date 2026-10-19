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
range) are computed directly from the file — exactly what DSH does against its
fs seam — so DSH's error texts can be reproduced with the real line numbers
before anything is mutated. All mutations still go through the Hermes tools so
Hermes' own guards, locks and approvals stay in force.
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


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


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
_READ_PAGE_LIMIT = 2000  # Hermes DEFAULT_READ_LIMIT


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

    forwarded = _parse(fwd.read_file({"path": path, "offset": 1, "limit": _READ_PAGE_LIMIT}, dict(kw)))
    try:
        content = _read_text(path)
    except (UnicodeDecodeError, OSError):
        if isinstance(forwarded, dict):
            if forwarded.get("error"):
                return str(forwarded["error"])
            if isinstance(forwarded.get("content"), str):
                return forwarded["content"]
        return 'cannot view "%s": not a regular file or directory' % path
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
    try:
        before = _read_text(path)
    except (UnicodeDecodeError, OSError):
        return C.err_edit_not_file(path)

    offsets = _match_offsets(before, old_value)
    if not offsets:
        return C.err_edit_not_found(path, old_value)
    if len(offsets) > 1:
        return C.err_edit_multiple(path, old_value, _line_numbers(before, offsets))

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

def _ensure_write_baseline(path: str, fwd: Forwarders, kw: dict):
    """Page a full read through Hermes so its write_file guard sees a fresh
    whole-file baseline; returns an error string or None."""
    offset = 1
    for _ in range(100):
        result = _parse(fwd.read_file({"path": path, "offset": offset, "limit": _READ_PAGE_LIMIT}, dict(kw)))
        if not isinstance(result, dict):
            return None  # non-JSON result — let the write guard decide
        if result.get("error"):
            return str(result["error"])
        next_offset = result.get("next_offset")
        if not next_offset:
            return None
        try:
            next_page = int(next_offset)
        except (TypeError, ValueError):
            return None
        if next_page <= offset:
            # No forward progress — treat as complete and let the write guard decide.
            return None
        offset = next_page
    return "Error: could not establish a full-file read baseline for %s (too many pages)." % path


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
    try:
        before = _read_text(path)
    except (UnicodeDecodeError, OSError):
        return C.err_insert_not_file(path)

    lines = before.split("\n")
    if (
        not isinstance(insert_line, int)
        or isinstance(insert_line, bool)
        or insert_line < 0
        or insert_line > len(lines)
    ):
        return C.err_insert_range(insert_line, len(lines))

    after = "\n".join(lines[:insert_line] + new_value.split("\n") + lines[insert_line:])
    baseline_error = _ensure_write_baseline(path, fwd, kw)
    if baseline_error:
        return baseline_error
    result = _parse(fwd.write_file({"path": path, "content": after}, dict(kw)))
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return C.ok_edited(path)