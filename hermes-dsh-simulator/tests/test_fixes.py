# -*- coding: utf-8 -*-
"""Regression tests for the dshsim editor/store fixes (standalone, no pytest).

Covers:
* F13 — the editor pre-check must read content through the host ``read_file``
  (a host guard/denial must win; a local file must never be opened directly).
* F14 — a stored takeover older than ``state.SESSION_TTL_DAYS`` is dropped at
  load and skipped by ``hydrate``.
* N35 — a GBK/ANSI file reports "not valid UTF-8" instead of "not a regular
  file or directory".
* N36 — concurrent ``state.add`` from several processes never loses an entry
  and never leaves an invalid JSON store.

Two modes:
    python tests/test_fixes.py              # fake-host unit tests (no Hermes)
    <hermes venv python> tests/test_fixes.py --host
                                            # same tests + real host read_file
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))

_PKG = os.path.basename(PLUGIN_ROOT)
translate = importlib.import_module("%s.dsh_modes.translate" % _PKG)
state = importlib.import_module("%s.dsh_modes.state" % _PKG)
registry = importlib.import_module("%s.dsh_modes.registry" % _PKG)

RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print("%s %s%s" % ("PASS" if condition else "FAIL", name, (" | " + detail) if detail else ""))


def _fwd(host, writes=None, patches=None):
    """Bind a Forwarders struct onto a fake (or real) host."""
    return translate.Forwarders(
        terminal=lambda args, kw: json.dumps({"output": "", "exit_code": 0}),
        read_file=host.read_file,
        write_file=host.write_file if hasattr(host, "write_file") else (lambda args, kw: json.dumps({"success": True})),
        patch=host.patch if hasattr(host, "patch") else (lambda args, kw: json.dumps({"success": True})),
    )


def _write_text(path: str, text: str) -> None:
    """Create the real file a fake host will serve (the stat pre-check needs it)."""
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


class FakeHost:
    """Mimics the real Hermes ``read_file``: ``<n>|`` gutter, paging through
    ``next_offset``, dedup stub on an identical repeated page, host error for
    files it refuses (guards, undecodable bytes)."""

    def __init__(self, files=None, errors=None, sizes=None, block_writes=False):
        self.files = dict(files or {})
        self.errors = dict(errors or {})
        self.sizes = dict(sizes or {})
        self.block_writes = block_writes
        self.calls = []
        self.writes = []
        self.patches = []
        self._seen = set()

    def read_file(self, args, kw):
        path = args["path"]
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or 2000))
        self.calls.append((path, offset, limit))
        if path in self.errors:
            return json.dumps(self.errors[path])
        text = self.files.get(path, "")
        lines = text.split("\n")
        page = lines[offset - 1:offset - 1 + limit]
        key = (path, offset, limit)
        if key in self._seen:
            return json.dumps({"status": "unchanged", "dedup": True, "content_returned": False, "path": path})
        self._seen.add(key)
        body = {
            "content": "\n".join("%d|%s" % (offset + index, line) for index, line in enumerate(page)),
            "total_lines": len(lines),
            "file_size": self.sizes.get(path, len(text.encode("utf-8"))),
        }
        end = offset + len(page) - 1
        if page and end < len(lines):
            body["next_offset"] = end + 1
            body["truncated"] = True
        return json.dumps(body)

    def write_file(self, args, kw):
        if self.block_writes:
            return json.dumps({"error": "Refusing to overwrite %s: partial view." % args["path"],
                               "stale_write_blocked": True, "path": args["path"]})
        self.writes.append((args["path"], args["content"]))
        self.files[args["path"]] = args["content"]
        return json.dumps({"success": True})

    def patch(self, args, kw):
        self.patches.append(args)
        return json.dumps({"success": True, "path": args["path"]})


# ---------------------------------------------------------------------------
# F13 — content comes from the host, never from a local open()
# ---------------------------------------------------------------------------

def test_view_obeys_host_guard(tmp: str) -> None:
    path = os.path.join(tmp, "guarded.txt")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("LOCAL-ONLY-SECRET\nsecond line\n")
    host = FakeHost(errors={path: {"error": "BLOCKED: denied by host read guard"}})
    out = translate.handle_editor({"command": "view", "path": path}, {}, _fwd(host))
    check("F13 view returns the host denial", "BLOCKED" in out, out[:80])
    check("F13 view never reads around the host", "LOCAL-ONLY-SECRET" not in out)


def test_edit_obeys_host_guard(tmp: str) -> None:
    path = os.path.join(tmp, "guarded_edit.txt")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("LOCAL-ONLY-SECRET = 1\n")
    host = FakeHost(errors={path: {"error": "BLOCKED: denied by host read guard"}})
    out = translate.handle_editor(
        {"command": "str_replace", "path": path, "old_str": "LOCAL-ONLY-SECRET", "new_str": "x"},
        {},
        _fwd(host),
    )
    check("F13 edit returns the host denial", "BLOCKED" in out, out[:80])
    check("F13 edit never patches around the host", not host.patches)


def test_view_pages_large_file(tmp: str) -> None:
    path = os.path.join(tmp, "big.txt")
    text = "\n".join("line %04d" % index for index in range(1, 5001)) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    host = FakeHost({path: text})
    body, exact, error = translate._host_read_all(path, _fwd(host), {})
    check("F13 paged read is lossless", exact and not error, "error=%s" % error)
    check("F13 paged read restores every byte", body == text, "%d vs %d chars" % (len(body or ""), len(text)))
    check("F13 paged read spans pages", len(host.calls) >= 3, "pages=%d" % len(host.calls))


def test_view_dedup_stub_retry(tmp: str) -> None:
    path = os.path.join(tmp, "dedup.txt")
    text = "alpha\nbeta\n"
    _write_text(path, text)
    host = FakeHost({path: text})
    translate._host_read_all(path, _fwd(host), {})          # first read: content served
    body, exact, error = translate._host_read_all(path, _fwd(host), {})
    check("F13 dedup stub is retried", exact and body == text, "error=%s" % error)


def test_insert_round_trip(tmp: str) -> None:
    path = os.path.join(tmp, "insert.txt")
    text = "one\ntwo\nthree\n"
    _write_text(path, text)
    host = FakeHost({path: text})
    out = translate.handle_editor(
        {"command": "insert", "path": path, "insert_line": 2, "new_str": "TWO-POINT-FIVE"},
        {},
        _fwd(host),
    )
    written = host.writes[-1][1] if host.writes else None
    check("F13 insert succeeds", "edited successfully" in out, out[:80])
    check("F13 insert keeps trailing newline + order",
          written == "one\ntwo\nTWO-POINT-FIVE\nthree\n", repr(written))


def test_fit_original_shapes() -> None:
    cases = [
        ("a\nb", 3, "a\nb"),
        ("a\nb", 9, "\ufeffa\r\nb\r\n"),
        ("one\ntwo\nthree\nfour", 23, "one\r\ntwo\r\nthree\r\nfour\r\n"),
        ("", 1, "\n"),
    ]
    for text, size, expected in cases:
        fitted, matched = translate._fit_original(text, size)
        check("F13 fit %r/%d" % (text, size), matched and fitted == expected, repr(fitted))
    fitted, matched = translate._fit_original("a\nb", 99)
    check("F13 fit refuses an impossible size", not matched and fitted == "a\nb")
    # 6 bytes is both "a\r\nb\r\n" and "\ufeffa\nb"; 4 is both "a\nb\n" and
    # "a\r\nb": never guess a byte shape for a caller that may rewrite the file.
    for size in (4, 6):
        fitted, matched = translate._fit_original("a\nb", size)
        check("F13 fit refuses the ambiguous %d-byte shape" % size,
              not matched and fitted == "a\nb", repr(fitted))


def test_insert_ambiguous_uses_patch(tmp: str) -> None:
    path = os.path.join(tmp, "ambiguous_insert.txt")
    with open(path, "wb") as handle:
        handle.write(b"one\r\ntwo\r\n")      # 10 bytes: CRLF text and BOM+LF text weigh the same
    # What the host hands back: CR stripped, no trailing newline, original size.
    host = FakeHost({path: "one\ntwo"}, sizes={path: 10})
    out = translate.handle_editor(
        {"command": "insert", "path": path, "insert_line": 1, "new_str": "one-and-a-half"},
        {},
        _fwd(host),
    )
    check("F13 ambiguous insert still lands", "edited successfully" in out, out[:80])
    check("F13 ambiguous insert splices, never rewrites", not host.writes, str(host.writes))
    check("F13 ambiguous insert anchors on the line above",
          bool(host.patches) and host.patches[-1]["old_string"] == "one"
          and host.patches[-1]["new_string"] == "one\none-and-a-half", str(host.patches[-1:]))


def test_insert_survives_host_baseline_refusal(tmp: str) -> None:
    path = os.path.join(tmp, "blocked_insert.txt")
    text = "alpha\nbeta\ngamma\ndelta\n"
    _write_text(path, text)
    host = FakeHost({path: text}, block_writes=True)
    out = translate.handle_editor(
        {"command": "insert", "path": path, "insert_line": 2, "new_str": "beta-and-a-half"},
        {},
        _fwd(host),
    )
    check("F13 insert falls back to a patch splice", "edited successfully" in out, out[:120])
    check("F13 splice used the host patch", bool(host.patches), str(host.patches[-1:]))
    check("F13 splice anchored on beta",
          bool(host.patches) and host.patches[-1]["old_string"] == "beta", str(host.patches[-1:]))


# ---------------------------------------------------------------------------
# N35 — non-UTF-8 is reported as such
# ---------------------------------------------------------------------------

_GBK_ERROR = {"is_binary": True, "error": "Binary file (unknown binary, 25 bytes) — cannot display as text.",
              "content": "", "total_lines": 0, "file_size": 25}


def test_gbk_messages(tmp: str) -> None:
    path = os.path.join(tmp, "gbk.txt")
    with open(path, "wb") as handle:
        handle.write("中文测试 GBK\n第二行\n".encode("gbk"))
    host = FakeHost(errors={path: _GBK_ERROR})
    out = translate.handle_editor({"command": "view", "path": path}, {}, _fwd(host))
    check("N35 view names the encoding", "not valid UTF-8" in out, out[:120])
    out = translate.handle_editor(
        {"command": "str_replace", "path": path, "old_str": "中文", "new_str": "x"}, {}, _fwd(host))
    check("N35 edit names the encoding", "not valid UTF-8" in out, out[:120])
    out = translate.handle_editor(
        {"command": "insert", "path": path, "insert_line": 0, "new_str": "x"}, {}, _fwd(host))
    check("N35 insert names the encoding", "not valid UTF-8" in out, out[:120])
    check("N35 no misleading 'not a regular file'", "not a regular file" not in out.lower())


def test_mojibake_content(tmp: str) -> None:
    path = os.path.join(tmp, "mojibake.txt")
    _write_text(path, "caf\ufffd\ufffd one\ntwo\n")
    # The host reports the ORIGINAL byte size (a GBK file), which the replacement
    # characters cannot reproduce — that mismatch is the undecodable signal.
    host = FakeHost({path: "caf\ufffd\ufffd one\ntwo\n"}, sizes={path: 17})
    out = translate.handle_editor({"command": "view", "path": path}, {}, _fwd(host))
    check("N35 replacement chars are reported", "not valid UTF-8" in out, out[:120])


# ---------------------------------------------------------------------------
# F14 — TTL on stored takeovers
# ---------------------------------------------------------------------------

def _iso(days_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_store_ttl(tmp: str) -> None:
    store = os.path.join(tmp, "dshsim-sessions.json")
    payload = {
        "version": 1,
        "updated_at": _iso(0),
        "sessions": {
            "stale_session": {"enabled_at": _iso(8), "surface": "desktop"},
            "fresh_session": {"enabled_at": _iso(0.5), "surface": "desktop"},
            "created_stale": {"created": _iso(30)},
            "undated_session": {"surface": "cli"},
        },
    }
    with open(store, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.environ["HERMES_DSHSIM_STORE"] = store
    state._cache["data"] = None
    state._cache["mtime"] = None
    keys = state.keys()
    check("F14 8-day entry dropped", "stale_session" not in keys, str(keys))
    check("F14 30-day created entry dropped", "created_stale" not in keys, str(keys))
    check("F14 fresh entry kept", "fresh_session" in keys, str(keys))
    check("F14 undated entry kept (fail open)", "undated_session" in keys, str(keys))
    armed = state.hydrate()
    check("F14 hydrate arms only live sessions", armed == 2, "armed=%s" % armed)
    check("F14 hydrate skipped the stale mode", registry.get_mode("stale_session") is None)
    check("F14 hydrate armed the fresh mode", registry.get_mode("fresh_session") == "minimal")


# ---------------------------------------------------------------------------
# N36 — concurrent writes
# ---------------------------------------------------------------------------

def _worker(prefix: str, count: int, store: str) -> None:
    os.environ["HERMES_DSHSIM_STORE"] = store
    state._cache["data"] = None
    state._cache["mtime"] = None
    for index in range(count):
        state.add("%s_%03d" % (prefix, index), surface="stress")


def test_store_concurrency(tmp: str) -> None:
    store = os.path.join(tmp, "concurrent.json")
    count = 40
    workers = 4
    env = dict(os.environ)
    env["HERMES_DSHSIM_STORE"] = store
    procs = [
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", "w%d" % index, str(count), store],
            env=env,
        )
        for index in range(workers)
    ]
    threads = [
        threading.Thread(target=_worker, args=("t%d" % index, count, store)) for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for proc in procs:
        proc.wait(timeout=180)
    for thread in threads:
        thread.join(timeout=180)

    with open(store, "r", encoding="utf-8") as handle:
        raw = handle.read()
    try:
        parsed = json.loads(raw)
        valid = True
    except Exception as exc:
        parsed, valid = {}, exc
    keys = set(parsed.get("sessions", {})) if valid else set()
    expected = {"w%d_%03d" % (index, n) for index in range(workers) for n in range(count)}
    expected |= {"t%d_%03d" % (index, n) for index in range(2) for n in range(count)}
    missing = expected - keys
    check("N36 store stays valid JSON", valid is True, str(valid))
    check("N36 every concurrent write survived", not missing, "missing=%d %s" % (len(missing), sorted(missing)[:5]))
    leftovers = [name for name in os.listdir(tmp) if name.endswith(".tmp")]
    check("N36 no temp leftovers", not leftovers, str(leftovers))


def test_store_lock_abandon(tmp: str) -> None:
    """A held lock makes ``_write`` give up instead of racing (returns False)."""
    store = os.path.join(tmp, "locked.json")
    os.environ["HERMES_DSHSIM_STORE"] = store
    state._cache["data"] = None
    state._cache["mtime"] = None
    holder_ready = threading.Event()
    holder_done = threading.Event()

    def hold() -> None:
        state._acquire()
        holder_ready.set()
        holder_done.wait(30)
        state._release()

    holder = threading.Thread(target=hold)
    holder.start()
    holder_ready.wait(10)
    started = time.time()
    ok = state._write({"version": 1, "updated_at": "", "sessions": {"x": {}}})
    elapsed = time.time() - started
    check("N36 busy lock abandons the write", ok is False, "ok=%s" % ok)
    check("N36 lock wait is bounded", elapsed <= 5.0, "%.2fs" % elapsed)
    holder_done.set()
    holder.join(10)
    check("N36 write succeeds once the lock frees",
          state._write({"version": 1, "updated_at": "", "sessions": {"x": {}}}) is True)


# ---------------------------------------------------------------------------
# Optional: the same content shapes through the REAL host read_file
# ---------------------------------------------------------------------------

def test_real_host(tmp: str) -> None:
    try:
        from tools.file_tools import _handle_patch, _handle_read_file, _handle_write_file
    except Exception as exc:  # not the Hermes runtime: skip loudly
        print("SKIP real-host integration (%s)" % exc)
        return
    fwd = translate.Forwarders(
        terminal=lambda args, kw: json.dumps({"output": "", "exit_code": 0}),
        read_file=lambda args, kw: _handle_read_file(dict(args), **dict(kw)),
        write_file=lambda args, kw: _handle_write_file(dict(args), **dict(kw)),
        patch=lambda args, kw: _handle_patch(dict(args), **dict(kw)),
    )

    crlf = os.path.join(tmp, "host_crlf.txt")
    with open(crlf, "wb") as handle:
        handle.write(b"line one\r\nline two\r\nline three\r\nline four\r\n")
    body, exact, error = translate._host_read_all(crlf, fwd, {})
    check("HOST CRLF round-trips byte-exactly", exact, "error=%s" % error)
    check("HOST CRLF keeps CR", body == "line one\r\nline two\r\nline three\r\nline four\r\n", repr(body))

    # A 2-line CRLF file weighs exactly as much as a BOM+LF one: the plugin must
    # report "not exact" instead of inventing a byte shape.
    tiny_crlf = os.path.join(tmp, "host_two_line_crlf.txt")
    with open(tiny_crlf, "wb") as handle:
        handle.write(b"one\r\ntwo\r\n")
    body, exact, error = translate._host_read_all(tiny_crlf, fwd, {})
    check("HOST short CRLF is reported ambiguous", exact is False, "exact=%s body=%r" % (exact, body))

    bom = os.path.join(tmp, "host_bom.txt")
    with open(bom, "wb") as handle:
        handle.write(b"\xef\xbb\xbfhello bom\n")
    body, exact, error = translate._host_read_all(bom, fwd, {})
    check("HOST BOM round-trips", exact and body == "\ufeffhello bom\n", repr(body))

    no_tail = os.path.join(tmp, "host_no_tail.txt")
    with open(no_tail, "wb") as handle:
        handle.write(b"a\nb")
    body, exact, error = translate._host_read_all(no_tail, fwd, {})
    check("HOST missing final newline preserved", exact and body == "a\nb", repr(body))

    gbk = os.path.join(tmp, "host_gbk.txt")
    with open(gbk, "wb") as handle:
        handle.write("中文测试 GBK\n第二行\n".encode("gbk"))
    out = translate.handle_editor({"command": "view", "path": gbk}, {}, fwd)
    check("HOST GBK view says not valid UTF-8", "not valid UTF-8" in out, out[:120])

    edit = os.path.join(tmp, "host_edit.txt")
    with open(edit, "wb") as handle:
        handle.write(b"alpha\r\nbeta\r\ngamma\r\ndelta\r\n")
    out = translate.handle_editor(
        {"command": "str_replace", "path": edit, "old_str": "beta", "new_str": "BETA"}, {}, fwd)
    with open(edit, "rb") as handle:
        after = handle.read()
    check("HOST edit lands through the host patch", "edited successfully" in out, out[:120])
    check("HOST edit keeps CRLF", after == b"alpha\r\nBETA\r\ngamma\r\ndelta\r\n", repr(after))

    ins = os.path.join(tmp, "host_insert.txt")
    with open(ins, "wb") as handle:
        handle.write(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    out = translate.handle_editor(
        {"command": "insert", "path": ins, "insert_line": 1, "new_str": "one-and-a-half"}, {}, fwd)
    with open(ins, "rb") as handle:
        after = handle.read()
    check("HOST insert lands", "edited successfully" in out, out[:400])
    check("HOST insert keeps CRLF",
          after == b"one\r\none-and-a-half\r\ntwo\r\nthree\r\nfour\r\n", repr(after))

    # Repeated edits on one file must not trip the host's repeated-read block:
    # each internal pre-check read uses a rotating page key.
    edits = os.path.join(tmp, "host_many_edits.txt")
    with open(edits, "wb") as handle:
        handle.write(b"mark 0\nmark 1\nmark 2\nmark 3\nmark 4\nmark 5\n")
    outcomes = []
    for index in range(6):
        outcomes.append(translate.handle_editor(
            {"command": "str_replace", "path": edits,
             "old_str": "mark %d" % index, "new_str": "MARK %d" % index}, {}, fwd))
    with open(edits, "rb") as handle:
        after = handle.read()
    blocked = [text for text in outcomes if "BLOCKED" in text]
    check("HOST six sequential edits all land", not blocked, str(blocked)[:160])
    check("HOST six edits produced the expected bytes",
          after == b"MARK 0\nMARK 1\nMARK 2\nMARK 3\nMARK 4\nMARK 5\n", repr(after))

    # A 2-line CRLF file is byte-ambiguous with a BOM+LF shape: insert must not
    # rewrite the file; it splices the new line in through the host patch.
    tiny = os.path.join(tmp, "host_tiny_crlf.txt")
    with open(tiny, "wb") as handle:
        handle.write(b"one\r\ntwo\r\n")
    out = translate.handle_editor(
        {"command": "insert", "path": tiny, "insert_line": 1, "new_str": "x"}, {}, fwd)
    with open(tiny, "rb") as handle:
        spliced = handle.read()
    check("HOST ambiguous insert splices instead of rewriting", "edited successfully" in out, out[:160])
    check("HOST ambiguous insert keeps the file's own EOLs",
          spliced == b"one\r\nx\r\ntwo\r\n", repr(spliced))


def main() -> int:
    if "--worker" in sys.argv:
        index = sys.argv.index("--worker")
        _worker(sys.argv[index + 1], int(sys.argv[index + 2]), sys.argv[index + 3])
        return 0
    real_host = "--host" in sys.argv
    with tempfile.TemporaryDirectory(prefix="dshsim-tests-") as tmp:
        test_view_obeys_host_guard(tmp)
        test_edit_obeys_host_guard(tmp)
        test_view_pages_large_file(tmp)
        test_view_dedup_stub_retry(tmp)
        test_insert_round_trip(tmp)
        test_fit_original_shapes()
        test_insert_ambiguous_uses_patch(tmp)
        test_insert_survives_host_baseline_refusal(tmp)
        test_gbk_messages(tmp)
        test_mojibake_content(tmp)
        test_store_ttl(tmp)
        test_store_concurrency(tmp)
        test_store_lock_abandon(tmp)
        if real_host:
            test_real_host(tmp)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
