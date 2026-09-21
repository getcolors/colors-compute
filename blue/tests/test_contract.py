import pytest
from colors_compute import credential_requirements, validate
from colors_compute.contract import registry, state_decision

def test_unreadable_state_is_never_a_first_create():
    with pytest.raises(ValueError, match="refusing mutation"):
        state_decision({"status": "error", "reason": "denied"}, "aws")
    assert state_decision({"status": "absent"}, "aws") == {"action": "create"}
    assert state_decision({"status": "present", "params": {"provider": "aws"}}, "aws") == {"action": "reuse"}


def test_legacy_and_provider_switch_require_explicit_action():
    with pytest.raises(ValueError, match="legacy state requires migration"):
        state_decision({"status": "present", "params": {}}, "aws")
    with pytest.raises(ValueError, match="set provider-compute back to vultr"):
        state_decision({"status": "present", "params": {"provider": "vultr"}}, "aws")


def test_exact_provider_and_backend_scope():
    assert set(registry()["compute"]) == {"azure", "aws", "google", "digitalocean", "hcloud", "vultr", "yandex", "oci"}
    assert set(registry()["backend"]) == {"r2", "s3", "gcs", "oci", "local"}
    assert validate({"provider-compute": "no-infra", "provider-backend": "unknown"})[:2] == [
        ":provider-compute must be one of aws, azure, digitalocean, google, hcloud, oci, vultr, yandex",
        ":provider-backend must be one of gcs, local, oci, r2, s3",
    ]


def test_credentials_do_not_turn_ambient_aws_into_colors_secrets():
    assert credential_requirements({"provider-compute": "aws", "provider-backend": "s3"}) == []
    assert credential_requirements({"provider-compute": "aws", "provider-backend": "r2"}) == [
        "COLORS_PAR_R2_ACCESS_KEY_ID", "COLORS_PAR_R2_SECRET_ACCESS_KEY",
    ]
    assert credential_requirements({"provider-compute": "digitalocean", "provider-backend": "s3"}) == ["COLORS_PAR_DO_TOKEN"]
