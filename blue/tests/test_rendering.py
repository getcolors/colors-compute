import json
from pathlib import Path

import pytest

from colors_compute import backend_plan, render_template


def test_template_retains_types_and_does_not_interpret_tofu_expressions():
    values = {"ids": ["one"], "enabled": True, "count": 3}
    result = render_template({"ids": "{{ids}}", "enabled": "{{enabled}}", "count": "{{count}}",
                              "reference": "${vultr_vpc.network.id}"}, values)
    assert result == {**values, "reference": "${vultr_vpc.network.id}"}
    result["ids"].append("two")
    assert values["ids"] == ["one"]


@pytest.mark.parametrize("template", ["prefix{{key}}", "{{missing}}", "{{broken-key}}"])
def test_missing_or_partial_placeholder_refused(template):
    with pytest.raises(ValueError):
        render_template(template, {})


def test_aws_compute_r2_plan_does_not_render_credentials_or_override_aws_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-fixture")
    opts = {"provider-compute": "aws", "provider-backend": "r2", "r2-bucket": "state",
            "r2-endpoint": "https://example.r2.cloudflarestorage.com",
            "r2-access-key-id": "fixture-secret-id", "r2-secret-access-key": "fixture-secret"}
    result = backend_plan(opts, "demo/compute/nodes/0.tfstate")
    assert result["environment"] == {}
    assert "fixture-secret" not in json.dumps(result)
    assert result["credential_bindings"] == {
        "COLORS_PAR_R2_ACCESS_KEY_ID": "access_key",
        "COLORS_PAR_R2_SECRET_ACCESS_KEY": "secret_key",
    }
    import os
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ambient-fixture"


def test_s3_uses_ambient_auth_and_lockfile():
    result = backend_plan({"provider-backend": "s3", "s3-bucket": "state", "s3-region": "us-east-1"}, "p/compute/shared.tfstate")
    assert result == {"config": {"terraform": {"backend": {"s3": {
        "bucket": "state", "region": "us-east-1", "key": "p/compute/shared.tfstate", "use_lockfile": True}}}},
        "credential_bindings": {}, "environment": {}}


@pytest.mark.parametrize("key", ["", "../other", "p//node", "/absolute", "p/../other"])
def test_backend_key_validation(key):
    with pytest.raises(ValueError, match="invalid state key"):
        backend_plan({"provider-backend": "s3", "s3-bucket": "state", "s3-region": "us-east-1"}, key)


def test_library_renderer_reproduces_vultr_provider_documents():
    # Repository-only contract test; packaging has a separate smoke check.
    root = Path(__file__).resolve().parents[2] / "providers/vultr"
    inputs = json.loads((root / "examples/inputs.json").read_text())
    for template, mode, name in [("node", "node", "main"), ("shared", "shared-optout", "main"),
                                 ("shared-keygen", "shared-keygen", "key")]:
        source = json.loads((root / f"{template}.tf.json.template").read_text())
        expected = json.loads((root / f"examples/{mode}/{name}.tf.json").read_text())
        assert render_template(source, inputs) == expected
