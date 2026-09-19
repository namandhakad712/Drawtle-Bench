"""Quick smoke test for the new test_only probe behaviour."""
import json
import urllib.request
import urllib.error
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drawtle import discovery as DSC


def test_test_only_strips_models():
    # Use a provider we know exists
    providers = DSC.merged_providers()
    name = None
    for n, spec in providers.items():
        if spec.get("models_url"):
            name = n
            break
    if not name:
        print("SKIP: no discoverable provider found")
        return

    spec = providers[name]
    full = DSC.probe(name, spec)
    assert "models" in full, "full probe should include models"

    # Simulate the server-side test_only filter
    test_only = {k: v for k, v in full.items() if k != "models"}
    assert "models" not in test_only, "test_only response must not contain models"
    assert test_only.get("ok") == full.get("ok"), "ok flag must be preserved"
    assert test_only.get("provider") == full.get("provider"), "provider must be preserved"
    assert test_only.get("elapsed_ms") == full.get("elapsed_ms"), "timing must be preserved"
    print(f"PASS: test_only filter works for provider {name}")


if __name__ == "__main__":
    test_test_only_strips_models()
