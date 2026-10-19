# -*- coding: utf-8 -*-
"""Binds the translation layer to the real Hermes tool handlers.

Imports are lazy (inside :func:`make_forwarders`) so the plugin package — and
its unit suite — stays importable without the Hermes runtime. Handlers are the
same functions the Hermes tool registry exposes, invoked with the same
``**kwargs`` context the executor hands a tool dispatch, so read-tracking,
write guards, approvals and terminal bookkeeping keep working exactly as for a
native tool call.

Forwarded handlers:
* ``bash``               -> ``tools.terminal_tool._handle_terminal``
* ``str_replace_editor`` -> ``tools.file_tools._handle_read_file`` / ``_handle_write_file`` / ``_handle_patch``
"""

from __future__ import annotations

from .translate import Forwarders


def make_forwarders() -> Forwarders:
    from tools.terminal_tool import _handle_terminal  # type: ignore
    from tools.file_tools import _handle_patch, _handle_read_file, _handle_write_file  # type: ignore

    def terminal(args: dict, kw: dict) -> str:
        return _handle_terminal(dict(args), **dict(kw))

    def read_file(args: dict, kw: dict) -> str:
        return _handle_read_file(dict(args), **dict(kw))

    def write_file(args: dict, kw: dict) -> str:
        return _handle_write_file(dict(args), **dict(kw))

    def patch(args: dict, kw: dict) -> str:
        return _handle_patch(dict(args), **dict(kw))

    return Forwarders(terminal=terminal, read_file=read_file, write_file=write_file, patch=patch)