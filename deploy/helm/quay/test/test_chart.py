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
        "--set",
        "migration.enabled=false",
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


@pytest.mark.parametrize("values_file", ["runtime.yaml", "migration.yaml"])
def test_image_digest_takes_precedence_for_workloads(values_file):
    resources = render(
        values_file,
        "--set-string",
        "image.tag=3.18.0",
        "--set-string",
        "image.digest=sha256:0123456789abcdef",
    )
    workloads = [resource for resource in resources if resource["kind"] in {"Deployment", "Job"}]
    assert workloads
    assert {
        container["image"]
        for workload in workloads
        for container in workload["spec"]["template"]["spec"]["containers"]
    } == {"quay.io/projectquay/quay@sha256:0123456789abcdef"}


@pytest.mark.parametrize("values_file", ["runtime.yaml", "migration.yaml", "ingress.yaml"])
def test_enabled_workload_requires_existing_config_secret(values_file):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "-f",
            str(CHART / "test/values" / values_file),
            "--set-string",
            "config.existingSecret=",
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "/config/existingSecret" in output


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


def test_runtime_renders_service_account_configuration():
    resources = render("runtime.yaml")
    service_account = by_kind(resources, "ServiceAccount")[0]
    assert service_account["metadata"]["annotations"] == {"example.com/workload-identity": "quay"}
    assert service_account["imagePullSecrets"] == [{"name": "true"}]


@pytest.mark.parametrize(
    ("component", "workload_label", "toleration_value"),
    [("app", "app", "app"), ("bkg-workers", "workers", "workers")],
)
def test_runtime_renders_pod_configuration(component, workload_label, toleration_value):
    deployment = next(
        item
        for item in by_kind(render("runtime.yaml"), "Deployment")
        if item["metadata"]["labels"]["app.kubernetes.io/component"] == component
    )
    pod_template = deployment["spec"]["template"]
    pod = pod_template["spec"]
    assert deployment["metadata"]["annotations"] == {
        "secret.reloader.stakater.com/reload": "quay-config"
    }
    assert pod_template["metadata"]["annotations"] == {
        "secret.reloader.stakater.com/reload": "quay-config"
    }
    assert pod_template["metadata"]["labels"]["example.com/workload"] == workload_label
    assert pod_template["metadata"]["labels"]["app.kubernetes.io/component"] == component
    assert pod["imagePullSecrets"] == [{"name": "123"}]
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["tolerations"] == [
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": toleration_value,
            "effect": "NoSchedule",
        }
    ]
    assert pod["topologySpreadConstraints"] == [
        {
            "maxSkew": 1,
            "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "ScheduleAnyway",
            "labelSelector": {"matchLabels": {"app.kubernetes.io/component": component}},
        }
    ]


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


def test_service_selectors_match_workload_pod_labels():
    resources = render("runtime.yaml")
    pod_labels_by_component = {
        deployment["spec"]["template"]["metadata"]["labels"][
            "app.kubernetes.io/component"
        ]: deployment["spec"]["template"]["metadata"]["labels"]
        for deployment in by_kind(resources, "Deployment")
    }
    services = by_kind(resources, "Service")
    assert services
    for service in services:
        component = service["metadata"]["labels"]["app.kubernetes.io/component"]
        assert service["spec"]["selector"].items() <= pod_labels_by_component[component].items()


@pytest.mark.parametrize("section", ["app", "workers"])
def test_deployments_reject_migration_capable_entrypoints(section):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "-f",
            str(CHART / "test/values/runtime.yaml"),
            "--set-string",
            f"{section}.entrypoint=registry",
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"/{section}/entrypoint" in output


def test_migration_only_mode():
    resources = render("migration.yaml")
    assert not by_kind(resources, "Deployment")
    assert {resource["kind"] for resource in resources} >= {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Job",
    }

    job = by_kind(resources, "Job")[0]
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert job["metadata"]["name"] == "test-quay-migration"
    assert job["metadata"]["labels"]["app.kubernetes.io/component"] == "db-migration"
    assert job["metadata"]["annotations"] == {"example.com/migration-purpose": "schema-upgrade"}
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    assert job["spec"]["backoffLimit"] == 1
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "test-quay"
    assert pod["nodeSelector"] == {"part-of": "quay"}
    assert pod["volumes"] == [{"name": "configvolume", "secret": {"secretName": "quay-config"}}]
    assert container["image"] == "quay.io/projectquay/quay:3.15.0"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["command"] == [
        "/quay-registry/quay-entrypoint.sh",
        "migrate",
        "head",
    ]
    assert container["volumeMounts"] == [{"name": "configvolume", "mountPath": "/conf/stack"}]
    assert container["resources"] == {
        "limits": {"memory": "2Gi"},
        "requests": {"cpu": "750m", "memory": "1Gi"},
    }
    environment = {item["name"]: item for item in container["env"]}
    assert environment["QE_K8S_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.namespace"
    )
    assert environment["QE_K8S_CONFIG_SECRET"]["value"] == "quay-config"
    assert environment["DEBUGLOG"]["value"] == "false"


def test_migration_image_override_replaces_common_image():
    job = by_kind(
        render(
            "migration.yaml",
            "--set-string",
            "migration.image=registry.example.com/quay@sha256:0123456789abcdef",
        ),
        "Job",
    )[0]
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example.com/quay@sha256:0123456789abcdef"
    )


@pytest.mark.parametrize("runtime_section", ["app", "workers"])
def test_migration_mode_rejects_runtime_workloads(runtime_section):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "-f",
            str(CHART / "test/values/migration.yaml"),
            "--set",
            f"{runtime_section}.enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"/{runtime_section}/enabled" in output


def test_optional_ingress_targets_app_service():
    ingress = by_kind(render("ingress.yaml"), "Ingress")[0]
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert ingress["metadata"]["annotations"] == {"example.com/provider-setting": "enabled"}
    assert ingress["spec"]["ingressClassName"] == "nginx"
    assert ingress["spec"]["rules"] == [
        {
            "host": "quay.example.com",
            "http": {
                "paths": [
                    {
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {
                            "service": {
                                "name": "test-quay-https",
                                "port": {"number": 443},
                            }
                        },
                    },
                    {
                        "path": "/v2",
                        "pathType": "Prefix",
                        "backend": {
                            "service": {
                                "name": "test-quay-https",
                                "port": {"number": 443},
                            }
                        },
                    },
                ]
            },
        }
    ]
    assert ingress["spec"]["tls"] == [
        {"hosts": ["quay.example.com"], "secretName": "quay-example-tls"}
    ]
    assert backend["name"] == "test-quay-https"
    assert backend["port"]["number"] == 443


def test_ingress_quotes_scalar_like_string_values():
    ingress = by_kind(
        render(
            "ingress.yaml",
            "--set-string",
            "ingress.className=true",
            "--set-string",
            "ingress.tls.secretName=123",
        ),
        "Ingress",
    )[0]
    assert ingress["spec"]["ingressClassName"] == "true"
    assert ingress["spec"]["tls"][0]["secretName"] == "123"


@pytest.mark.parametrize("values_file", ["runtime.yaml", "migration.yaml", "ingress.yaml"])
def test_chart_does_not_render_secret_management_resources(values_file):
    resources = render(values_file)
    assert all(
        resource["kind"]
        not in {
            "ExternalSecret",
            "SecretStore",
            "ClusterSecretStore",
            "VaultAuth",
            "VaultConnection",
            "VaultStaticSecret",
        }
        for resource in resources
    )
    assert all(
        not resource["apiVersion"].startswith(
            (
                "external-secrets.io/",
                "generators.external-secrets.io/",
                "secrets.hashicorp.com/",
            )
        )
        for resource in resources
    )


@pytest.mark.parametrize(
    ("values_file", "setting", "unknown_property"),
    [
        ("migration.yaml", "migration.targetRevisoin=head", "targetRevisoin"),
        ("migration.yaml", "migration.resources.requestz.cpu=1", "requestz"),
        ("migration.yaml", "migration.environment.DEBUGLOGG=false", "DEBUGLOGG"),
        ("ingress.yaml", "ingress.classNmae=nginx", "classNmae"),
        ("ingress.yaml", "ingress.tls.secretNmae=tls", "secretNmae"),
    ],
)
def test_task4_closed_objects_reject_unknown_properties(values_file, setting, unknown_property):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "-f",
            str(CHART / "test/values" / values_file),
            "--set",
            setting,
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"additional properties '{unknown_property}' not allowed" in output


@pytest.mark.parametrize(
    ("setting", "unknown_property"),
    [
        ("image.repositroy=example.com/quay", "repositroy"),
        ("config.existingSecrett=quay-config", "existingSecrett"),
        ("serviceAccount.creat=true", "creat"),
        ("app.replicaCout=2", "replicaCout"),
        ("app.strategy.rollingUpdate.maxSurgee=1", "maxSurgee"),
        ("app.resources.requestz.cpu=1", "requestz"),
        ("app.startupProbe.periodSecondz=30", "periodSecondz"),
        ("app.environment.DEBUGLOGG=false", "DEBUGLOGG"),
        ("app.services.https.targetPorrt=8443", "targetPorrt"),
        ("workers.replicaCout=2", "replicaCout"),
        ("workers.strategy.rollingUpdate.maxSurgee=1", "maxSurgee"),
        ("workers.resources.requestz.cpu=1", "requestz"),
        ("workers.readinessProbe.periodSecondz=30", "periodSecondz"),
        ("workers.environment.DEBUGLOGG=false", "DEBUGLOGG"),
        ("workers.services.grpc.targetPorrt=55443", "targetPorrt"),
    ],
)
def test_core_closed_objects_reject_unknown_properties(setting, unknown_property):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "--set",
            setting,
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"additional properties '{unknown_property}' not allowed" in output


@pytest.mark.parametrize("section", ["app", "workers"])
@pytest.mark.parametrize(
    "reserved_label",
    [
        "app\\.kubernetes\\.io/name",
        "app\\.kubernetes\\.io/instance",
        "app\\.kubernetes\\.io/component",
    ],
)
def test_pod_labels_reject_reserved_selector_keys(section, reserved_label):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "--set-string",
            f"{section}.podLabels.{reserved_label}=override",
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"invalid propertyName '{reserved_label.replace('\\.', '.')}'" in output


@pytest.mark.parametrize(
    ("setting", "schema_path"),
    [
        ("app.enabled=false", "/app/enabled"),
        ("app.services.https.enabled=false", "/app/services/https/enabled"),
    ],
)
def test_ingress_requires_app_https_service(setting, schema_path):
    result = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "-f",
            str(CHART / "test/values/ingress.yaml"),
            "--set",
            setting,
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert schema_path in output


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


@pytest.mark.parametrize("values_file", ["runtime.yaml", "migration.yaml"])
def test_user_controlled_names_remain_strings(values_file):
    resources = render(
        values_file,
        "--set-string",
        "serviceAccount.name=true",
        "--set-string",
        "config.existingSecret=123",
    )
    service_account = by_kind(resources, "ServiceAccount")[0]
    role_binding = by_kind(resources, "RoleBinding")[0]
    workloads = [resource for resource in resources if resource["kind"] in {"Deployment", "Job"}]
    assert service_account["metadata"]["name"] == "true"
    assert role_binding["subjects"][0]["name"] == "true"
    assert workloads
    for workload in workloads:
        pod = workload["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "true"
        assert pod["volumes"][0]["secret"]["secretName"] == "123"


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
