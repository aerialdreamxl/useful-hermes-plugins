"""Bridge loader for hermes-provider-advanced under model-providers."""
from __future__ import annotations
import logging
import sys
from pathlib import Path

logger = logging.getLogger("hermes.advanced_providers.bridge")

try:
    from hermes_advlib.provider_profile import build_and_register_advanced_profile
    build_and_register_advanced_profile()
except Exception:
    try:
        here = Path(__file__).resolve()
        cands = [
            here.parent.parent / "custom-plugins" / "hermes-provider-advanced",
            here.parent.parent / "hermes-provider-advanced",
            here.parent.parent.parent / "hermes-provider-advanced",
        ]
        for cand in cands:
            if (cand / "advlib").is_dir():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break
        from advlib.provider_profile import build_and_register_advanced_profile
        build_and_register_advanced_profile()
    except Exception as exc:
        logger.debug("advanced provider bridge failed to load: %s", exc)
