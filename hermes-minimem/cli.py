"""CLI surface: ``hermes hermes-minimem <command>``.

The top-level command name is the ACTIVE provider's name (``_attach_plugin_cli_command``
does ``subparsers.add_parser(cmd_info["name"])`` where name is the provider), so it is
"hermes-minimem", not "minimem" — and the command only exists while that provider is the
configured one (``discover_plugin_cli_commands`` filters by ``memory.provider``).

Loaded by ``discover_plugin_cli_commands`` only for the ACTIVE memory provider, and only
for its argparse setup — so imports here must stay cheap and must not require numpy until
a command actually needs the index.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional


def _provider():
    """Build a throwaway provider bound to the current HERMES_HOME."""
    # The CLI scanner registers the parent package before loading this file, so the
    # relative import resolves in the real framework path.
    from .provider import MinimemProvider

    provider = MinimemProvider()
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    provider.initialize("cli", hermes_home=hermes_home, agent_context="cron")
    return provider


def _fmt_stats(stats: dict) -> str:
    lines = [
        f"entries        {stats.get('entries')}",
        f"db             {stats.get('db_bytes', 0) / 1024:.1f} KB  ({stats.get('db_path')})",
        f"backend        {stats.get('backend')}  output={stats.get('output_type')}  key={stats.get('api_key_source')}",
        f"model          {stats.get('model')} @ {stats.get('dim')}d",
        f"sparse         {stats.get('sparse_mode')}"
        f"{' (effective: %s)' % stats.get('sparse_mode_effective') if stats.get('sparse_mode_effective') != stats.get('sparse_mode') else ''}"
        f"  (bm25 docs {stats.get('bm25_docs')}, vocab {stats.get('bm25_vocab')})",
        f"dense          {'ready' if stats.get('dense_available') else 'degraded'}"
        f"{'' if stats.get('numpy_ok') else ' (numpy missing: %s)' % stats.get('numpy_error')}",
    ]
    if stats.get("sparse_mode") == "model":
        lines.append(
            f"model sparse   {'ready' if stats.get('model_sparse_available') else 'degraded'}"
            f"  (docs {stats.get('model_sparse_docs')})"
        )
    lines.append(f"needs_reembed  {stats.get('needs_reembed')}")
    if stats.get("config_invalid"):
        lines.append(f"config         INVALID: {stats['config_invalid']}")
    return "\n".join(lines)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Attach the ``minimem`` subcommands. Called by the framework during argparse setup."""
    subparser.add_argument(
        "minimem_action",
        nargs="?",
        default="stats",
        choices=["stats", "search", "reindex", "reembed", "export", "path"],
        help="stats (default) | search <query> | reindex | reembed [file.jsonl] | export <file> | path",
    )
    subparser.add_argument("minimem_arg", nargs="?", default="", help="query text, export path, or import JSONL path")
    subparser.add_argument("--limit", type=int, default=8, help="search: max results")
    subparser.add_argument("--json", action="store_true", help="machine-readable output")
    subparser.add_argument("--batch", type=int, default=8, help="reembed: texts per embedding request (sidecar caps flash at 10; server 500s at 20)")
    subparser.add_argument("--dry-run", action="store_true", help="reembed: count targets, embed nothing")
    subparser.add_argument("--every", action="store_true", help="reembed: all rows, not just stale ones")
    subparser.add_argument(
        "--default-kind", default="fact",
        help="reembed import file: kind for records lacking a valid one",
    )
    subparser.set_defaults(func=minimem_command)


def minimem_command(args: argparse.Namespace) -> int:
    action = getattr(args, "minimem_action", None) or "stats"
    provider = _provider()
    try:
        if action == "stats":
            stats = provider.stats()
            print(json.dumps(stats, ensure_ascii=False, indent=2) if getattr(args, "json", False) else _fmt_stats(stats))
            return 0
        if action == "path":
            print(provider._store.path if provider._store else "")
            return 0
        if action == "search":
            query = getattr(args, "minimem_arg", "") or ""
            if not query.strip():
                print("usage: hermes hermes-minimem search <query>", file=sys.stderr)
                return 2
            hits = provider.recall(query, limit=max(1, int(getattr(args, "limit", 8))), include_scores=True)
            if getattr(args, "json", False):
                print(json.dumps(hits, ensure_ascii=False, indent=2))
            else:
                if not hits:
                    print("(no matches)")
                for i, hit in enumerate(hits, 1):
                    print(f"{i}. [{hit['kind']}] {hit['score']:.3f}  {hit['id']}")
                    print(f"   {hit['text'][:160]}")
            return 0
        if action == "reindex":
            provider._reindex_all()
            sparse = f", model-sparse {provider._sparse_index.count}" if provider._settings and provider._settings.uses_model_sparse else ""
            print(f"reindexed: {provider._bm25.size} documents{sparse}")
            return 0
        if action == "reembed":
            path = getattr(args, "minimem_arg", "") or ""
            if path:
                records = _load_jsonl(path)
                result = provider.import_documents(
                    records, batch=max(1, int(getattr(args, "batch", 8))),
                    default_kind=getattr(args, "default_kind", "fact"), verbose=True,
                )
            else:
                result = provider.reembed_all(
                    batch=max(1, int(getattr(args, "batch", 8))),
                    dry_run=bool(getattr(args, "dry_run", False)),
                    every=bool(getattr(args, "every", False)), verbose=True,
                )
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif not result.get("ok"):
                print(f"reembed failed: {result.get('error')}", file=sys.stderr)
            else:
                if path:
                    print(f"imported {result['imported']}/{result['total']} "
                          f"(dupes skipped {result['skipped_dupe']}, errors {result['errors']})")
                else:
                    verb = "would re-embed" if result["dry_run"] else "re-embedded"
                    print(f"{verb} {result['updated']}/{result['total']} rows (errors {result['errors']})")
            return 0 if result.get("ok") else 1
        if action == "export":
            path = getattr(args, "minimem_arg", "") or ""
            if not path:
                print("usage: hermes hermes-minimem export <file.jsonl>", file=sys.stderr)
                return 2
            count = _export(provider, path)
            print(f"exported {count} entries -> {path}")
            return 0
        print(f"unknown action {action}", file=sys.stderr)
        return 2
    finally:
        provider.shutdown()


def _load_jsonl(path: str) -> list:
    """Read ``{text, kind?, importance?, source?}`` records, one per JSON line."""
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON line: {exc}")
            if not isinstance(rec, dict):
                raise SystemExit(f"{path}:{line_no}: expected a JSON object, got {type(rec).__name__}")
            records.append(rec)
    return records


def _export(provider, path: str) -> int:
    """JSONL of text + metadata. Vectors are deliberately omitted: they are model-bound."""
    store = provider._store
    if store is None:
        return 0
    rows = store.all_ids_texts()
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for mem_id, _text, _imp in rows:
            entry = store.get(mem_id)
            if not entry:
                continue
            entry.pop("vec", None)
            entry.pop("sparse", None)
            entry.pop("sparse_json", None)
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1
    return count
