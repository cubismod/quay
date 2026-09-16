import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).parents[1]


def render(values_file: str, *helm_args: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(CHART),
            "-f",
            str(CHART / "test/values" / values_file),
            *helm_args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def helper_probe(*helm_args: str) -> dict:
    resources = render(
        "runtime.yaml",
        "--skip-schema-validation",
        "--set",
        "_testRenderHelpers=true",
        *helm_args,
    )
    assert len(resources) == 1
    return resources[0]


def test_chart_metadata():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart == {
        "apiVersion": "v2",
        "name": "quay",
        "description": "Deploy Quay registry workloads",
        "type": "application",
        "version": "0.1.0",
        "appVersion": "latest",
    }


def test_workload_entrypoints_default_to_registry_nomigrate():
    probe = helper_probe()
    assert probe["data"]["appEntrypoint"] == "registry-nomigrate"
    assert probe["data"]["workerEntrypoint"] == "registry-nomigrate"


def test_helpers_render_naming_and_labels():
    probe = helper_probe()
    assert probe["metadata"] == {
        "name": "test-quay",
        "labels": {
            "helm.sh/chart": "quay-0.1.0",
            "app.kubernetes.io/name": "quay",
            "app.kubernetes.io/instance": "test",
            "app.kubernetes.io/version": "latest",
            "app.kubernetes.io/managed-by": "Helm",
        },
    }
    assert yaml.safe_load(probe["data"]["selectorLabels"]) == {
        "app.kubernetes.io/name": "quay",
        "app.kubernetes.io/instance": "test",
    }


def test_image_helper_renders_tag():
    probe = helper_probe("--set-string", "image.tag=3.18.0")
    assert probe["data"]["image"] == "quay.io/projectquay/quay:3.18.0"


def test_image_helper_prefers_digest_over_tag():
    probe = helper_probe(
        "--set-string",
        "image.tag=3.18.0",
        "--set-string",
        "image.digest=sha256:0123456789abcdef",
    )
    assert probe["data"]["image"] == ("quay.io/projectquay/quay@sha256:0123456789abcdef")


@pytest.mark.parametrize("section", ["app", "workers"])
def test_workload_entrypoints_require_strings(section):
    result = subprocess.run(
        ["helm", "lint", str(CHART), "--set", f"{section}.entrypoint=123"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"at '/{section}/entrypoint': got number, want string" in output
