#!/usr/bin/env python3
"""Check real refresh-only JSON semantics using disposable local files, no cloud."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="compute-connection-plan-") as directory:
        root = Path(directory)
        target = root / "machine.txt"
        config = {
            "terraform": {"required_providers": {"local": {
                "source": "hashicorp/local", "version": "2.5.3"}}},
            "resource": {"local_file": {"node": {
                "filename": str(target), "content": "owned machine"}}},
            "output": {"identity": {"value": "${local_file.node.id}"}},
        }
        (root / "main.tf.json").write_text(json.dumps(config))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TF_", "TOFU_", "COLORS_PAR_"))}

        def run(*args):
            result = subprocess.run([os.environ.get("TOFU", "tofu"), *args],
                                    cwd=root, env=env, capture_output=True, text=True,
                                    timeout=300)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout

        run("init", "-input=false", "-no-color")
        # Only the temporary local file is created; this configuration has no cloud.
        run("apply", "-auto-approve", "-input=false", "-no-color")
        original_state = (root / "terraform.tfstate").read_bytes()

        def refreshed_resources():
            run("plan", "-refresh-only", "-input=false", "-no-color",
                "-out=connection.tfplan")
            plan = json.loads(run("show", "-json", "connection.tfplan"))
            assert not plan.get("errored"), "refresh plan errored"
            assert (root / "terraform.tfstate").read_bytes() == original_state
            # prior_state is the provider-refreshed snapshot, not stored state.
            return plan.get("prior_state", {}).get("values", {}).get(
                "root_module", {}).get("resources", [])

        observed = refreshed_resources()
        assert len(observed) == 1 and observed[0]["type"] == "local_file"
        target.unlink()
        assert not refreshed_resources(), "deleted file survived provider refresh"
        assert json.loads(original_state)["resources"], "stored state must retain ownership"
        print("Refresh-only JSON: live resources and out-of-band deletion verified; state unchanged")


if __name__ == "__main__":
    main()
