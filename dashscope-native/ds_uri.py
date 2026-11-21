"""dashscope://<region>[@<workspace-id>] URI parsing for qwen_dashscope.

Grammar is the contract in PROMPT.md §3.3 — region first, workspace second,
single ``@`` separator. Non-dashscope URLs return None (not ours); malformed
dashscope URLs raise with the grammar printed, never fall through silently.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

SCHEME = "dashscope://"

# dashscope 2.0.4 _config.VALID_REGIONS (verified against the wheel).
VALID_REGIONS = (
    "cn-beijing",
    "ap-southeast-1",
    "cn-hongkong",
    "us-east-1",
    "eu-central-1",
    "ap-northeast-1",
)

_USAGE = (
    "expected dashscope://<region>[@<workspace-id>], "
    "e.g. dashscope://cn-beijing@llm-xxxxxxxxxxxxxxxx "
    "(valid regions: " + ", ".join(VALID_REGIONS) + ")"
)


class DashScopeUriError(ValueError):
    """Malformed dashscope:// reference — always carries the usage hint."""


@dataclass(frozen=True)
class DashScopeRef:
    region: str
    workspace: Optional[str]  # None → SDK legacy shared gateway (it warns itself)

    @property
    def gateway_host(self) -> str:
        if self.workspace:
            return f"{self.workspace}.{self.region}.maas.aliyuncs.com"
        return ""


def parse_dashscope_uri(base_url: str) -> Optional[DashScopeRef]:
    """Parse a dashscope:// base_url; None when the URL is not ours."""
    raw = str(base_url or "").strip()
    if not raw.lower().startswith(SCHEME):
        return None
    body = raw[len(SCHEME):].strip().rstrip("/")
    if not body:
        raise DashScopeUriError(f"empty dashscope reference in {raw!r} — {_USAGE}")
    if "/" in body or "\\" in body:
        raise DashScopeUriError(f"path segments not allowed in dashscope reference {raw!r} — {_USAGE}")
    if body.count("@") > 1:
        raise DashScopeUriError(f"multiple '@' in dashscope reference {raw!r} — {_USAGE}")
    region_part, _, workspace_part = body.partition("@")
    region = region_part.strip().lower()
    workspace = workspace_part.strip() or None
    if not region:
        raise DashScopeUriError(f"missing region in {raw!r} — {_USAGE}")
    if region not in VALID_REGIONS:
        raise DashScopeUriError(f"unknown region {region!r} in {raw!r} — {_USAGE}")
    if workspace is not None and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", workspace)
            or workspace.startswith("-") or workspace.endswith("-")):
        raise DashScopeUriError(f"invalid workspace id in {raw!r} — {_USAGE}")
    return DashScopeRef(region=region, workspace=workspace)
