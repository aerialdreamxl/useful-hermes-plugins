# -*- coding: utf-8 -*-
"""DSH minimal-mode model-facing surface, copied 1:1 from the DSH reference source.

Sources (all read from ``D:/02-DIY/hermes-plugin-maker/dsh-reference-src``):

* ``packages/bundle/web-app/presets/minimal.patch.yml``             -> web bash description + persona prefix
* ``snapshots/web/minimal-preset/tool-schemas.expected.json``       -> web minimal bash schema
* ``snapshots/web/minimal-preset/system-prompt.expected.md``        -> web prompt (''极简模式'' / RL variant)
* ``snapshots/sdk/persistent-tools/tool-schemas.expected.json``     -> sdk two-tool roster (bash + str_replace_editor)
* ``snapshots/sdk/persistent-tools/system-prompt.expected.md``      -> sdk prompt
* ``packages/fs/tool-str-replace-editor/src/index.ts``              -> editor schema + result/error texts
* ``packages/shell/tool-bash-persistent/src/index.ts``              -> bash result rendering texts

Do NOT hand-edit the strings below: ``tests/test_unit.py`` byte-compares them
against the snapshot JSON/markdown files above (skip-if-missing, so the test
still runs on machines without the reference checkout).

Flavor: DSH ships two closely-related minimal variants. ``web`` (default) is
the ''极简模式'' agent preset: persona ``You are a helpful software engineer
assistant.`` + the RL-environment bash description. ``sdk`` is the bare
two-tool SDK runtime: ``You are the environment-selected minimal software
engineer.`` + the internet-less bash description. Both present
``bash`` + ``str_replace_editor``. Switch with the env var
``HERMES_DSHSIM_FLAVOR=web|sdk``; the system prompt additionally honors
``DSH_SYSTEM_PROMPT`` (mirrors DSH's own env override).
"""

from __future__ import annotations

import copy
import os

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

WEB_PROMPT = "You are a helpful software engineer assistant."
SDK_PROMPT = "You are the environment-selected minimal software engineer."

PROMPTS = {"web": WEB_PROMPT, "sdk": SDK_PROMPT}

# ---------------------------------------------------------------------------
# bash tool schema (both flavors; only the description differs)
# ---------------------------------------------------------------------------

_WEB_BASH_DESC = (
    "Run commands in a bash shell\n"
    "* When invoking this tool, the contents of the \"command\" parameter does NOT need to be XML-escaped.\n"
    "* Network access depends on the task environment. Prefer configured mirrors/proxies when they are available.\n"
    "* State is persistent across command calls and discussions with the user.\n"
    "* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.\n"
    "* Please avoid commands that may produce a very large amount of output.\n"
    "* Please run long lived commands in the background, e.g. 'sleep 10 &' or start a server in the background."
)

_SDK_BASH_DESC = (
    "Run commands in a bash shell\n"
    "* When invoking this tool, the contents of the \"command\" parameter does NOT need to be XML-escaped.\n"
    "* You don't have access to the internet via this tool.\n"
    "* You do have access to a mirror of common linux and python packages via apt and pip.\n"
    "* State is persistent across command calls and discussions with the user.\n"
    "* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.\n"
    "* Please avoid commands that may produce a very large amount of output.\n"
    "* Please run long lived commands in the background, e.g. 'sleep 10 &' or start a server in the background."
)


def _bash_schema(description: str) -> dict:
    return {
        "name": "bash",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run. Relative path is preferred in the command.",
                }
            },
            "required": ["command"],
        },
    }


BASH_SCHEMAS = {"web": _bash_schema(_WEB_BASH_DESC), "sdk": _bash_schema(_SDK_BASH_DESC)}

# ---------------------------------------------------------------------------
# str_replace_editor tool schema (identical in every DSH deployment that mounts it)
# ---------------------------------------------------------------------------

EDITOR_DESC = (
    "Custom editing tool for viewing, creating and editing files\n"
    "* State is persistent across command calls and discussions with the user\n"
    "* If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep\n"
    "* The `create` command cannot be used if the specified `path` already exists as a file\n"
    "* If a `command` generates a long output, it will be truncated and marked with `<response clipped>`\n"
    "* A null placeholder for a parameter unused by the selected command is treated as omitted. Required parameters still need values; omit `str_replace.new_str` rather than setting it to null when deleting a match\n"
    "\n"
    "Notes for using the `str_replace` command:\n"
    "* The `old_str` parameter should match EXACTLY one or more consecutive lines from the original file. Be mindful of whitespaces!\n"
    "* If the `old_str` parameter is not unique in the file, the replacement will not be performed. Make sure to include enough context in `old_str` to make it unique\n"
    "* The `new_str` parameter should contain the edited lines that should replace the `old_str`"
)

EDITOR_SCHEMA = {
    "name": "str_replace_editor",
    "description": EDITOR_DESC,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The commands to run. Allowed options are: `view`, `create`, `str_replace`, `insert`.",
                "enum": ["view", "create", "str_replace", "insert"],
            },
            "path": {
                "type": "string",
                "description": "Absolute path to file or directory, e.g. `/repo/file.py` or `/repo`.",
            },
            "file_text": {
                "oneOf": [{"type": "string"}, {"type": "null"}],
                "description": "Required string parameter of `create` command, with the content of the file to be created. A null placeholder is treated as omitted by commands that do not use this parameter.",
            },
            "insert_line": {
                "oneOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Required integer parameter of `insert` command. The `new_str` will be inserted AFTER the line `insert_line` of `path`. A null placeholder is treated as omitted by commands that do not use this parameter.",
            },
            "new_str": {
                "oneOf": [{"type": "string"}, {"type": "null"}],
                "description": "Optional string parameter of `str_replace` command containing the new string (if omitted, no string will be added). Required string parameter of `insert` command containing the string to insert. A null placeholder is accepted only by commands that do not use this parameter.",
            },
            "old_str": {
                "oneOf": [{"type": "string"}, {"type": "null"}],
                "description": "Required string parameter of `str_replace` command containing the string in `path` to replace. A null placeholder is treated as omitted by commands that do not use this parameter.",
            },
            "view_range": {
                "oneOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "null"},
                ],
                "description": "Optional parameter of `view` command when `path` points to a file. If omitted or null, the full file is shown. If provided, the file will be shown in the indicated line number range, e.g. [11, 12] will show lines 11 and 12. Indexing at 1 to start. Setting `[start_line, -1]` shows all lines from `start_line` to the end of the file.",
            },
        },
        "required": ["command", "path"],
    },
}

# ---------------------------------------------------------------------------
# bash result-rendering texts (from tool-bash-persistent/src/index.ts)
# ---------------------------------------------------------------------------

# Not produced by our forwarder (Hermes has no scrollback loss); kept for completeness.
LOST_PREFIX_MESSAGE = "<response clipped><NOTE>The beginning of this command output was dropped by the terminal scrollback limit. The following text is the earliest retained output.</NOTE>\n"
TRUNCATED_MESSAGE = "<response clipped><NOTE>To save on context only part of this file has been shown to you. You should retry this tool after you have searched inside the file with `grep -n` in order to find the line numbers of what you are looking for.</NOTE>"
SHELL_RESET_MESSAGE = "The persistent bash shell was reset; the next bash call starts from the workspace with a fresh current directory and environment."
TIMEOUT_STATUS_MARKER = "[Command timed out or OOM]"


def finished_marker(exit_code: int) -> str:
    return "[Command finished with exit code %s]" % exit_code


def timeout_intro(seconds: int) -> str:
    return (
        "Your command timed out after %s seconds or experienced an OOM error. "
        "Below is partial output:" % seconds
    )


# ---------------------------------------------------------------------------
# str_replace_editor result/error texts (from tool-str-replace-editor/src/index.ts)
# ---------------------------------------------------------------------------

ERR_PATH_EMPTY = "path must be a non-empty string"


def err_not_absolute(path: str) -> str:
    return "The path %s is not an absolute path, it should start with `/`. Maybe you meant /%s?" % (path, path)


def err_not_exist(path: str) -> str:
    return "The path %s does not exist. Please provide a valid path." % path


def err_is_dir(path: str) -> str:
    return "The path %s is a directory and only the `view` command can be used on directories" % path


def err_required(param: str, command: str) -> str:
    return "Parameter `%s` is required for command: %s" % (param, command)


def err_empty(param: str, command: str) -> str:
    return "Parameter `%s` is empty for command: %s" % (param, command)


ERR_NEW_STR_NULL = "Parameter `new_str` must be omitted or contain a string for command: str_replace"


def err_create_exists(path: str) -> str:
    return "File already exists at: %s. Cannot overwrite files using command `create`." % path


def ok_created(path: str) -> str:
    return "New file created successfully at: %s" % path


def err_edit_not_found(path: str, old_value: str) -> str:
    return "No replacement was performed, old_str `%s` did not appear verbatim in %s." % (old_value, path)


def err_edit_multiple(path: str, old_value: str, lines) -> str:
    return (
        "No replacement was performed. Multiple occurrences of old_str `%s` in lines [%s]. "
        "Please ensure it is unique" % (old_value, ", ".join(str(n) for n in lines))
    )


def err_edit_not_file(path: str) -> str:
    return 'cannot edit "%s": not a regular file' % path


def err_insert_not_file(path: str) -> str:
    return 'cannot insert into "%s": not a regular file' % path


def err_insert_range(insert_line, total_lines: int) -> str:
    return (
        "Invalid `insert_line` parameter: %s. It should be within the range of lines "
        "of the file: [0, %s]" % (insert_line, total_lines)
    )


def ok_edited(path: str) -> str:
    return "The file %s has been edited successfully." % path


def err_view_range_shape(view_range) -> str:
    return "Invalid `view_range`. It should be a list of two integers."


def err_view_range_start(view_range, initial_line: int, total_lines: int) -> str:
    return (
        "Invalid `view_range`: [%s]. Its first element `%s` should be within the range "
        "of lines of the file: [1, %s]" % (", ".join(str(x) for x in view_range), initial_line, total_lines)
    )


def err_view_range_end(view_range, final_line: int, total_lines: int) -> str:
    return (
        "Invalid `view_range`: [%s]. Its second element `%s` should be smaller than the number "
        "of lines in the file: `%s`" % (", ".join(str(x) for x in view_range), final_line, total_lines)
    )


def err_view_range_order(view_range, final_line: int, initial_line: int) -> str:
    return (
        "Invalid `view_range`: [%s]. Its second element `%s` should be larger or equal than "
        "its first `%s`" % (", ".join(str(x) for x in view_range), final_line, initial_line)
    )


def view_header(path: str, total_lines: int, view_range=None) -> str:
    head = "Here's the content of %s with line numbers (which has a total of %s lines)" % (path, total_lines)
    if view_range is not None:
        head += " with view_range=[%s, %s]" % (view_range[0], view_range[1])
    return head


def dir_header(path: str) -> str:
    return (
        "Here're the files and directories up to 2 levels deep in %s, excluding hidden "
        "items, node_modules, and Python cache directories:" % path
    )


# ---------------------------------------------------------------------------
# Active flavor resolution
# ---------------------------------------------------------------------------

FLAVOR_ENV = "HERMES_DSHSIM_FLAVOR"
PROMPT_ENV = "DSH_SYSTEM_PROMPT"  # same name DSH itself uses for its SDK override
MAX_OUTPUT_ENV = "HERMES_DSHSIM_MAX_OUTPUT_CHARS"
# v0.4.0 rename (hermes-dsh-modes -> hermes-dsh-simulator); the old names still read.
LEGACY_FLAVOR_ENV = "HERMES_DSH_MODES_FLAVOR"
LEGACY_MAX_OUTPUT_ENV = "HERMES_DSH_MODES_MAX_OUTPUT_CHARS"


def _env_new_then_legacy(name: str, legacy: str) -> str:
    return os.environ.get(name) or os.environ.get(legacy) or ""

DEFAULT_FLAVOR = "web"
DEFAULT_MAX_OUTPUT_CHARS = 40000
BASH_TIMEOUT_S = 300  # DSH minimal configures timeoutMs: 300000


def active_flavor() -> str:
    value = (_env_new_then_legacy(FLAVOR_ENV, LEGACY_FLAVOR_ENV) or DEFAULT_FLAVOR).strip().lower()
    return value if value in ("web", "sdk") else DEFAULT_FLAVOR


def active_prompt() -> str:
    override = os.environ.get(PROMPT_ENV)
    if override:
        return override
    return PROMPTS[active_flavor()]


def active_bash_schema() -> dict:
    return copy.deepcopy(BASH_SCHEMAS[active_flavor()])


def editor_schema() -> dict:
    return copy.deepcopy(EDITOR_SCHEMA)


def active_max_output_chars() -> int:
    raw = _env_new_then_legacy(MAX_OUTPUT_ENV, LEGACY_MAX_OUTPUT_ENV)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_CHARS