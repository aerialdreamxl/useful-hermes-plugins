"""Persistent dependency-isolated embedding worker for hermes-minimem.

This file is never imported by the main Hermes process. It is run by a PM side-environment
interpreter and only needs stdlib plus the selected backend package (dashscope or fastembed).
State is retained process-wide so a local ONNX model is not reloaded on every memory recall.
"""
from __future__ import annotations

import hashlib
import json
import sys
import traceback


LOCAL_UNSUPPORTED_SPARSE = {
    "Qdrant/bm25",
    "Qdrant/bm42-all-minilm-l6-v2-attentions",
}
STATE = {"signature": None, "dense": None, "sparse": None, "dashscope_client": None}


def _config_signature(config: dict) -> str:
    safe = {k: v for k, v in config.items() if k != "api_key"}
    return hashlib.sha256(json.dumps(safe, sort_keys=True).encode("utf-8")).hexdigest()


def _redact(message: str, config: dict) -> str:
    text = str(message)
    key = str(config.get("api_key") or "")
    if key:
        text = text.replace(key, "[redacted]")
    return text[:3000]


def _sparse_pairs(raw) -> list:
    if raw is None:
        return []
    if hasattr(raw, "indices") and hasattr(raw, "values"):
        return [[int(i), float(v)] for i, v in zip(raw.indices, raw.values)]
    if isinstance(raw, dict):
        return [[int(k), float(v)] for k, v in raw.items()]
    pairs = []
    for item in raw:
        if isinstance(item, dict):
            pairs.append([int(item["index"]), float(item["value"])])
        elif hasattr(item, "index") and hasattr(item, "value"):
            pairs.append([int(item.index), float(item.value)])
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pairs.append([int(item[0]), float(item[1])])
        else:
            raise ValueError("unrecognised sparse embedding entry")
    return pairs


def _normalize_dense(raw) -> list:
    if raw is None:
        return []
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    return [float(value) for value in raw]


def _ensure_fastembed(config: dict) -> None:
    import fastembed

    signature = _config_signature(config)
    if STATE["signature"] != signature:
        STATE.update({"signature": signature, "dense": None, "sparse": None})
    wants_dense = config["output_type"] in ("dense", "dense&sparse")
    wants_sparse = config["output_type"] in ("sparse", "dense&sparse")
    cache_dir = config.get("local_cache_dir") or None
    if wants_dense and STATE["dense"] is None:
        STATE["dense"] = fastembed.TextEmbedding(
            model_name=config["model"], cache_dir=cache_dir,
        )
    if wants_sparse:
        model_name = str(config.get("local_sparse_model") or "").strip()
        if not model_name:
            raise ValueError("local_sparse_model is required for sparse output")
        if model_name in LOCAL_UNSUPPORTED_SPARSE:
            raise ValueError(
                f"{model_name} needs corpus IDF at index time; choose a learned sparse model "
                "(e.g. prithivida/Splade_PP_en_v1)"
            )
        if STATE["sparse"] is None:
            STATE["sparse"] = fastembed.SparseTextEmbedding(
                model_name=model_name, cache_dir=cache_dir,
            )


def _embed_local(config: dict, texts: list) -> list:
    _ensure_fastembed(config)
    wants_dense = config["output_type"] in ("dense", "dense&sparse")
    wants_sparse = config["output_type"] in ("sparse", "dense&sparse")
    dense_items = list(STATE["dense"].embed(texts, batch_size=32)) if wants_dense else [None] * len(texts)
    sparse_items = list(STATE["sparse"].embed(texts, batch_size=32)) if wants_sparse else [None] * len(texts)
    out = []
    for dense, sparse in zip(dense_items, sparse_items):
        dense_list = _normalize_dense(dense)
        sparse_pairs = _sparse_pairs(sparse)
        if wants_dense and not dense_list:
            raise ValueError("local dense embedding returned no values")
        if wants_sparse and not sparse_pairs:
            raise ValueError("local sparse embedding returned no values")
        out.append({"dense": dense_list if wants_dense else None,
                    "sparse": sparse_pairs if wants_sparse else None})
    return out


def _ensure_dashscope(config: dict):
    import dashscope

    signature = _config_signature(config)
    if STATE["signature"] != signature:
        STATE.update({"signature": signature, "dashscope_client": None})
    if STATE["dashscope_client"] is None:
        kwargs = {"api_key": config["api_key"], "timeout": float(config.get("text_timeout") or 60)}
        if config.get("workspace"):
            kwargs["workspace"] = config["workspace"]
            if config.get("region"):
                kwargs["region"] = config["region"]
        elif config.get("base_url"):
            kwargs["base_url"] = config["base_url"]
        STATE["dashscope_client"] = dashscope.Client(**kwargs)
    return STATE["dashscope_client"]


def _embed_dashscope(config: dict, texts: list, text_type: str) -> list:
    client = _ensure_dashscope(config)
    kwargs = {
        "model": config["model"],
        "input": texts,
        "text_type": text_type,
        "output_type": config["output_type"],
    }
    if int(config.get("dimension") or 0) > 0:
        kwargs["dimension"] = int(config["dimension"])
    if config.get("instruct"):
        kwargs["instruct"] = config["instruct"]
    response = client.embeddings.create(**kwargs)
    items = list(getattr(response, "embeddings", []) or [])
    if len(items) != len(texts):
        raise ValueError(f"DashScope returned {len(items)} embeddings for {len(texts)} texts")
    items.sort(key=lambda item: int(getattr(item, "index", 0) or 0))
    wants_dense = config["output_type"] in ("dense", "dense&sparse")
    wants_sparse = config["output_type"] in ("sparse", "dense&sparse")
    out = []
    for item in items:
        dense = _normalize_dense(getattr(item, "embedding", None))
        sparse = _sparse_pairs(getattr(item, "sparse_embedding", None))
        if wants_dense and not dense:
            raise ValueError("DashScope returned an empty dense embedding")
        if wants_sparse and not sparse:
            raise ValueError("DashScope returned an empty sparse embedding")
        out.append({"dense": dense if wants_dense else None,
                    "sparse": sparse if wants_sparse else None})
    return out


def handle(request: dict) -> dict:
    config = request.get("config") or {}
    backend = str(config.get("backend") or "")
    texts = [str(text) for text in request.get("texts") or []]
    text_type = str(request.get("text_type") or "document")
    if not texts:
        return {"ok": True, "result": []}
    if backend == "dashscope":
        return {"ok": True, "result": _embed_dashscope(config, texts, text_type)}
    if backend == "local":
        return {"ok": True, "result": _embed_local(config, texts)}
    raise ValueError(f"unsupported sidecar backend: {backend}")


def main() -> None:
    for line in sys.stdin:
        request_id = None
        config = {}
        try:
            request = json.loads(line)
            request_id = request.get("id")
            config = request.get("config") or {}
            if request.get("op") != "embed":
                raise ValueError(f"unknown sidecar operation: {request.get('op')}")
            response = handle(request)
        except Exception as exc:
            response = {"ok": False, "error": _redact(f"{type(exc).__name__}: {exc}", config)}
            # m7: format_exc must run INSIDE the handler — after the except block Python
            # clears exc_info and print_exc() only yields "NoneType: None".
            traceback.print_exc()
        response["id"] = request_id
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
