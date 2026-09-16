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
        "migration.yaml",
        "--skip-schema-validation",
        "--set",
        "_testRenderHelpers=true",
        *helm_args,
    )
    assert len(resources) == 1
    return resources[0]


def by_kind(resources, kind):
    return [resource for resource in resources if resource["kind"] == kind]


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


def test_runtime_renders_app_and_shared_resources():
    resources = render("runtime.yaml")
    deployment = next(
        item
        for item in by_kind(resources, "Deployment")
        if item["metadata"]["name"] == "test-quay-app"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert deployment["spec"]["replicas"] == 2
    assert container["image"] == "quay.io/projectquay/quay:3.15.0"
    assert container["command"] == [
        "/quay-registry/quay-entrypoint.sh",
        "registry-nomigrate",
    ]
    assert (
        deployment["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"]
        == "quay-config"
    )
    assert {item["kind"] for item in resources} >= {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Service",
    }


def test_app_preserves_runtime_behavior():
    resources = render("runtime.yaml")
    deployment = by_kind(resources, "Deployment")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    assert deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "quay",
        "app.kubernetes.io/instance": "test",
        "app.kubernetes.io/component": "app",
    }
    assert pod["serviceAccountName"] == "test-quay"
    assert pod["terminationGracePeriodSeconds"] == 60
    assert pod["nodeSelector"] == {"part-of": "quay"}
    assert pod["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0][
        "podAffinityTerm"
    ]["labelSelector"]["matchExpressions"][0] == {
        "key": "app.kubernetes.io/component",
        "operator": "In",
        "values": ["app"],
    }
    assert container["volumeMounts"] == [{"name": "configvolume", "mountPath": "/conf/stack"}]
    assert container["lifecycle"]["preStop"]["exec"]["command"] == [
        "/bin/sh",
        "-c",
        "sleep 20 && kill -QUIT $(cat /tmp/nginx.pid)",
    ]
    assert container["startupProbe"] == {
        "failureThreshold": 20,
        "timeoutSeconds": 60,
        "periodSeconds": 60,
        "httpGet": {"path": "/health/instance", "port": 8443, "scheme": "HTTPS"},
    }
    assert container["readinessProbe"] == {
        "failureThreshold": 3,
        "successThreshold": 1,
        "initialDelaySeconds": 15,
        "periodSeconds": 30,
        "timeoutSeconds": 10,
        "httpGet": {"path": "/health/endtoend", "port": 8443, "scheme": "HTTPS"},
    }
    assert container["livenessProbe"] == {
        "failureThreshold": 3,
        "periodSeconds": 10,
        "tcpSocket": {"port": 8443},
    }
    assert container["resources"] == {
        "limits": {"memory": "2Gi"},
        "requests": {"cpu": "500m", "memory": "1Gi"},
    }
    environment = {item["name"]: item for item in container["env"]}
    assert environment["QE_K8S_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.namespace"
    )
    assert environment["QE_K8S_CONFIG_SECRET"]["value"] == "quay-config"
    assert {
        name: environment[name]["value"]
        for name in {
            "DEBUGLOG",
            "IGNORE_VALIDATION",
            "QUAY_LOGGING",
            "WORKER_CONNECTION_COUNT_REGISTRY",
            "DB_CONNECTION_POOLING",
            "WORKER_COUNT_WEB",
            "WORKER_COUNT_REGISTRY",
            "QUAY_SERVICES",
            "QUAY_OVERRIDE_SERVICES",
        }
    } == {
        "DEBUGLOG": "false",
        "IGNORE_VALIDATION": "false",
        "QUAY_LOGGING": "stdout",
        "WORKER_CONNECTION_COUNT_REGISTRY": "50",
        "DB_CONNECTION_POOLING": "true",
        "WORKER_COUNT_WEB": "4",
        "WORKER_COUNT_REGISTRY": "28",
        "QUAY_SERVICES": "",
        "QUAY_OVERRIDE_SERVICES": "",
    }


def test_app_services_have_independent_flags_and_app_selectors():
    resources = render("runtime.yaml", "--set", "app.services.metrics.enabled=false")
    services = [
        service
        for service in by_kind(resources, "Service")
        if service["metadata"]["labels"]["app.kubernetes.io/component"] == "app"
    ]
    assert [service["metadata"]["name"] for service in services] == ["test-quay-https"]
    assert services[0]["spec"]["ports"] == [
        {"name": "https", "protocol": "TCP", "port": 443, "targetPort": 8443}
    ]
    assert services[0]["spec"]["selector"]["app.kubernetes.io/component"] == "app"

    resources = render("runtime.yaml", "--set", "app.services.https.enabled=false")
    services = [
        service
        for service in by_kind(resources, "Service")
        if service["metadata"]["labels"]["app.kubernetes.io/component"] == "app"
    ]
    assert [service["metadata"]["name"] for service in services] == ["test-quay-metrics"]
    assert services[0]["spec"]["ports"] == [
        {"name": "metrics", "protocol": "TCP", "port": 9091, "targetPort": 9091}
    ]
    assert services[0]["spec"]["selector"]["app.kubernetes.io/component"] == "app"


def test_runtime_renders_separate_workers():
    resources = render("runtime.yaml")
    worker = next(
        item
        for item in by_kind(resources, "Deployment")
        if item["metadata"]["name"] == "test-quay-workers"
    )
    container = worker["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == [
        "/quay-registry/quay-entrypoint.sh",
        "registry-nomigrate",
    ]
    assert any(env["name"] == "QUAY_OVERRIDE_SERVICES" for env in container["env"])
    assert (
        worker["spec"]["selector"]["matchLabels"]
        != next(
            item
            for item in by_kind(resources, "Deployment")
            if item["metadata"]["name"] == "test-quay-app"
        )["spec"]["selector"]["matchLabels"]
    )


def test_workers_preserve_runtime_behavior():
    resources = render("runtime.yaml")
    deployment = next(
        item
        for item in by_kind(resources, "Deployment")
        if item["metadata"]["name"] == "test-quay-workers"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["minReadySeconds"] == 0
    assert deployment["spec"]["progressDeadlineSeconds"] == 600
    assert deployment["spec"]["revisionHistoryLimit"] == 10
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    assert deployment["spec"]["selector"] == {
        "matchLabels": {
            "app.kubernetes.io/name": "quay",
            "app.kubernetes.io/instance": "test",
            "app.kubernetes.io/component": "bkg-workers",
        }
    }
    assert pod["serviceAccountName"] == "test-quay"
    assert pod["terminationGracePeriodSeconds"] == 60
    assert pod["nodeSelector"] == {"part-of": "quay"}
    assert pod["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0][
        "podAffinityTerm"
    ]["labelSelector"]["matchExpressions"][0] == {
        "key": "app.kubernetes.io/component",
        "operator": "In",
        "values": ["bkg-workers"],
    }
    assert pod["volumes"] == [{"name": "configvolume", "secret": {"secretName": "quay-config"}}]
    assert container["image"] == "quay.io/projectquay/quay:3.15.0"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["ports"] == [
        {"name": "grpc", "containerPort": 55443},
        {"name": "metrics", "containerPort": 9091},
    ]
    assert container["volumeMounts"] == [{"name": "configvolume", "mountPath": "/conf/stack"}]
    assert container["startupProbe"] == {
        "failureThreshold": 10,
        "periodSeconds": 15,
        "tcpSocket": {"port": 50051},
    }
    assert container["readinessProbe"] == {
        "failureThreshold": 3,
        "successThreshold": 1,
        "periodSeconds": 15,
        "tcpSocket": {"port": 50051},
    }
    assert container["livenessProbe"] == {
        "failureThreshold": 5,
        "periodSeconds": 15,
        "tcpSocket": {"port": 50051},
    }
    assert container["resources"] == {
        "limits": {"memory": "4096Mi"},
        "requests": {"cpu": "1", "memory": "4096Mi"},
    }
    environment = {item["name"]: item for item in container["env"]}
    assert environment["QE_K8S_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.namespace"
    )
    assert environment["QE_K8S_CONFIG_SECRET"]["value"] == "quay-config"
    assert {
        name: environment[name]["value"]
        for name in {
            "DEBUGLOG",
            "IGNORE_VALIDATION",
            "QUAY_LOGGING",
            "DB_CONNECTION_POOLING",
            "QUAY_SERVICES",
            "QUAY_OVERRIDE_SERVICES",
        }
    } == {
        "DEBUGLOG": "false",
        "IGNORE_VALIDATION": "false",
        "QUAY_LOGGING": "stdout",
        "DB_CONNECTION_POOLING": "true",
        "QUAY_SERVICES": "",
        "QUAY_OVERRIDE_SERVICES": (
            "gunicorn-registry=false,gunicorn-web=false,gunicorn-secscan=false"
        ),
    }


def test_worker_services_have_independent_flags_and_worker_selectors():
    resources = render("runtime.yaml", "--set", "workers.services.metrics.enabled=false")
    services = [
        service
        for service in by_kind(resources, "Service")
        if service["metadata"]["labels"]["app.kubernetes.io/component"] == "bkg-workers"
    ]
    assert [service["metadata"]["name"] for service in services] == ["test-quay-workers-grpc"]
    assert services[0]["spec"]["ports"] == [
        {"name": "grpc", "protocol": "TCP", "port": 443, "targetPort": 55443}
    ]
    assert services[0]["spec"]["selector"]["app.kubernetes.io/component"] == "bkg-workers"

    resources = render("runtime.yaml", "--set", "workers.services.grpc.enabled=false")
    services = [
        service
        for service in by_kind(resources, "Service")
        if service["metadata"]["labels"]["app.kubernetes.io/component"] == "bkg-workers"
    ]
    assert [service["metadata"]["name"] for service in services] == ["test-quay-workers-metrics"]
    assert services[0]["spec"]["ports"] == [
        {"name": "metrics", "protocol": "TCP", "port": 9091, "targetPort": 9091}
    ]
    assert services[0]["spec"]["selector"]["app.kubernetes.io/component"] == "bkg-workers"


def test_shared_rbac_uses_existing_service_account():
    resources = render(
        "runtime.yaml",
        "--set",
        "serviceAccount.create=false",
        "--set-string",
        "serviceAccount.name=existing-quay",
    )
    assert not by_kind(resources, "ServiceAccount")
    deployment = by_kind(resources, "Deployment")[0]
    role = by_kind(resources, "Role")[0]
    role_binding = by_kind(resources, "RoleBinding")[0]
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == ("existing-quay")
    assert role_binding["subjects"] == [{"kind": "ServiceAccount", "name": "existing-quay"}]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get", "patch", "update"],
        },
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"]},
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "verbs": ["get", "list", "patch", "update", "watch"],
        },
    ]


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
