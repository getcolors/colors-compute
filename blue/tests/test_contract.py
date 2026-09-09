import pytest

from colors_compute import collect, credential_requirements, expand, state_decision, state_keys, validate
from colors_compute.contract import registry


def node(node_id, **extra):
    return {"node_id": node_id, "provider": "vultr", "name": f"cluster-{node_id}",
            "ip": "192.0.2.10", "user": "root", "sudoer": "root", **extra}


def test_join_orders_by_request_and_preserves_metadata_without_mutating_input():
    requests = expand([{"role": "broker", "count": 2}])
    results = [node("broker-1", metadata={"disk": "disk-2"}), node("broker-0", uid="1000")]
    result = collect(requests, results, "broker-0")
    assert [n["node_id"] for n in result["nodes"]] == ["broker-0", "broker-1"]
    assert result["nodes"][1]["metadata"] == {"disk": "disk-2"}
    assert result["nodes"][0]["uid"] == "1000"
    assert "role" not in results[0]


@pytest.mark.parametrize("results,message", [
    ([node("0")], "missing node: 1"),
    ([node("0"), node("0")], "duplicate node: 0"),
    ([node("2")], "undeclared node: 2"),
    ([node("0"), node("1", ip="")], "incomplete node 1: ip"),
    ([node("0"), node("1", provider="aws")], "provider mismatch: 1"),
])
def test_join_refuses_partial_or_ambiguous_cluster(results, message):
    with pytest.raises(ValueError, match=message):
        collect(expand([{"role": None, "count": 2}]), results, "0")


def test_private_network_requires_each_private_address():
    with pytest.raises(ValueError, match="incomplete node 0: vpc_ip"):
        collect([{"node_id": "0", "private": True}], [node("0")], "0")


def test_scaling_keeps_identity_and_state_keys():
    small = expand([{"role": "broker", "count": 1}])
    large = expand([{"role": "broker", "count": 3}])
    assert small[0] == large[0]
    a = state_keys("example", [n["node_id"] for n in small])
    b = state_keys("example", [n["node_id"] for n in large])
    assert a["nodes"]["broker-0"] == b["nodes"]["broker-0"]


@pytest.mark.parametrize("profile,ids", [("../other", ["0"]), ("example", ["../node"]), ("example", ["0", "0"])])
def test_state_keys_refuse_collision_and_path_traversal(profile, ids):
    with pytest.raises(ValueError):
        state_keys(profile, ids)


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
    assert set(registry()["backend"]) == {"r2", "s3"}
    assert validate({"provider-compute": "no-infra", "provider-backend": "local"})[:2] == [
        ":provider-compute must be one of aws, azure, digitalocean, google, hcloud, oci, vultr, yandex",
        ":provider-backend must be one of r2, s3",
    ]


def test_credentials_do_not_turn_ambient_aws_into_colors_secrets():
    assert credential_requirements({"provider-compute": "aws", "provider-backend": "s3"}) == []
    assert credential_requirements({"provider-compute": "aws", "provider-backend": "r2"}) == [
        "COLORS_PAR_R2_ACCESS_KEY_ID", "COLORS_PAR_R2_SECRET_ACCESS_KEY",
    ]
    assert credential_requirements({"provider-compute": "digitalocean", "provider-backend": "s3"}) == ["COLORS_PAR_DO_TOKEN"]


@pytest.mark.parametrize("count", [True, False, 0, -1, 1.5, "3"])
def test_counts_do_not_accept_coercion(count):
    with pytest.raises(ValueError, match="count must be a positive integer"):
        expand([{"role": "worker", "count": count}])
