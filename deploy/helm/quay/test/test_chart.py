import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).parents[1]


def render(values_file: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(CHART),
            "-f",
            str(CHART / "test/values" / values_file),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


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
