# Quay Workload Helm Chart

This chart renders the Kubernetes workloads needed to run Quay. It supports application and background-worker Deployments, a migration-only Job, and an optional provider-neutral Ingress. All workloads are disabled by default.

## Requirements

- Helm 4
- An existing Kubernetes Secret containing the Quay configuration
- Backing services, such as PostgreSQL, Redis, and object storage, configured in that Secret

The chart does not create the Quay configuration Secret. Set `config.existingSecret` whenever any workload is enabled. The Secret must contain the Quay configuration as `config.yaml`; it is mounted at `/conf/stack`, and its name is passed to Quay through `QE_K8S_CONFIG_SECRET`.

## Render Examples

Run these examples from the repository root. They use committed fixture values and do not install resources:

```bash
# Application and background-worker runtime
helm template quay-runtime deploy/helm/quay \
  -f deploy/helm/quay/test/values/runtime.yaml

# Migration Job without runtime Deployments
helm template quay-migration deploy/helm/quay \
  -f deploy/helm/quay/test/values/migration.yaml

# Application runtime with an Ingress
helm template quay-ingress deploy/helm/quay \
  -f deploy/helm/quay/test/values/ingress.yaml
```

Use a production values file to install a mode:

```bash
helm upgrade --install quay deploy/helm/quay -f /path/to/values.yaml
```

## Installation Modes

### Runtime

Set `app.enabled`, `workers.enabled`, or both to `true`. Runtime Deployments always default to the non-migrating `registry-nomigrate` entrypoint. Run database migrations separately before updating runtime workloads when an upgrade requires them.

The application exposes independently configurable HTTPS and metrics Services. Background workers expose independently configurable gRPC and metrics Services.

### Migration Only

Set `migration.enabled: true` while leaving `app.enabled` and `workers.enabled` false. The resulting Job runs:

```text
/quay-registry/quay-entrypoint.sh migrate <migration.targetRevision>
```

The migration Job uses the same image, configuration Secret, ServiceAccount, and release-scoped RBAC as runtime workloads. Argo CD hook or sync-wave annotations are not supplied by default; inject orchestration annotations through the consuming GitOps configuration when needed.

### Optional Ingress

Set `ingress.enabled: true`, `app.enabled: true`, and `app.services.https.enabled: true`. Configure one host and one or more Prefix paths. TLS is optional; enabling it requires an existing TLS Secret name. The Ingress targets the application's HTTPS Service and contains no provider-specific configuration.

## Image Selection

Set `image.repository` and either `image.tag` or `image.digest`. A non-empty digest takes precedence over the tag and renders an immutable `repository@digest` image reference:

```yaml
image:
  repository: quay.io/projectquay/quay
  tag: "3.18.0"
  digest: sha256:0123456789abcdef
```

## Configuration Reloading

This chart neither installs Reloader nor adds Reloader annotations. A deployment system such as `quay-gitops` can inject the annotation into each rendered Deployment when automatic restarts after Secret changes are required:

```yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
```

Inject the annotation on the Deployment metadata, not the pod template. The Reloader controller and its permissions remain the responsibility of the target environment.

## Values

The JSON schema in `values.schema.json` validates the values contract. Resource maps, node selectors, affinity, and supported annotations remain Kubernetes-native mappings.

### Shared Values

| Value | Default | Description |
| --- | --- | --- |
| `image.repository` | `quay.io/projectquay/quay` | Quay image repository. |
| `image.tag` | `latest` | Image tag used when `image.digest` is empty. |
| `image.digest` | `""` | Image digest; takes precedence over the tag. |
| `image.pullPolicy` | `IfNotPresent` | Container image pull policy. |
| `config.existingSecret` | `""` | Existing Quay configuration Secret; required when a workload is enabled. |
| `serviceAccount.create` | `true` | Create a release-scoped ServiceAccount. |
| `serviceAccount.name` | `""` | Existing ServiceAccount name; required when creation is disabled. |

### Application Values

| Value | Default | Description |
| --- | --- | --- |
| `app.enabled` | `false` | Render the application Deployment and enabled Services. |
| `app.replicaCount` | `1` | Application replica count. |
| `app.entrypoint` | `registry-nomigrate` | Fixed non-migrating Quay entrypoint for Deployments. |
| `app.minReadySeconds` | `0` | Minimum ready time for new pods. |
| `app.progressDeadlineSeconds` | `600` | Deployment progress deadline. |
| `app.revisionHistoryLimit` | `10` | Retained ReplicaSets. |
| `app.terminationGracePeriodSeconds` | `60` | Pod termination grace period. |
| `app.strategy.rollingUpdate.maxUnavailable` | `0` | Maximum unavailable pods during rollout. |
| `app.strategy.rollingUpdate.maxSurge` | `1` | Maximum surge pods during rollout. |
| `app.resources` | See `values.yaml` | Container resource requests and limits. |
| `app.startupProbe` | See `values.yaml` | HTTPS startup probe timing. |
| `app.readinessProbe` | See `values.yaml` | HTTPS readiness probe timing. |
| `app.livenessProbe` | See `values.yaml` | TCP liveness probe timing. |
| `app.environment` | See `values.yaml` | Supported Quay runtime environment values. |
| `app.nodeSelector` | `{part-of: quay}` | Pod node selector. |
| `app.affinity` | See `values.yaml` | Pod affinity configuration. |
| `app.services.https.enabled` | `true` | Render the application HTTPS Service. |
| `app.services.https.port` | `443` | HTTPS Service port. |
| `app.services.https.targetPort` | `8443` | HTTPS container port. |
| `app.services.metrics.enabled` | `true` | Render the application metrics Service. |
| `app.services.metrics.port` | `9091` | Metrics Service port. |
| `app.services.metrics.targetPort` | `9091` | Metrics container port. |

### Worker Values

| Value | Default | Description |
| --- | --- | --- |
| `workers.enabled` | `false` | Render the background-worker Deployment and enabled Services. |
| `workers.replicaCount` | `1` | Worker replica count. |
| `workers.entrypoint` | `registry-nomigrate` | Fixed non-migrating Quay entrypoint for Deployments. |
| `workers.minReadySeconds` | `0` | Minimum ready time for new pods. |
| `workers.progressDeadlineSeconds` | `600` | Deployment progress deadline. |
| `workers.revisionHistoryLimit` | `10` | Retained ReplicaSets. |
| `workers.terminationGracePeriodSeconds` | `60` | Pod termination grace period. |
| `workers.strategy.rollingUpdate.maxUnavailable` | `0` | Maximum unavailable pods during rollout. |
| `workers.strategy.rollingUpdate.maxSurge` | `1` | Maximum surge pods during rollout. |
| `workers.resources` | See `values.yaml` | Container resource requests and limits. |
| `workers.startupProbe` | See `values.yaml` | TCP startup probe timing. |
| `workers.readinessProbe` | See `values.yaml` | TCP readiness probe timing. |
| `workers.livenessProbe` | See `values.yaml` | TCP liveness probe timing. |
| `workers.environment` | See `values.yaml` | Supported Quay worker environment values. |
| `workers.nodeSelector` | `{part-of: quay}` | Pod node selector. |
| `workers.affinity` | See `values.yaml` | Pod affinity configuration. |
| `workers.services.grpc.enabled` | `true` | Render the worker gRPC Service. |
| `workers.services.grpc.port` | `443` | gRPC Service port. |
| `workers.services.grpc.targetPort` | `55443` | gRPC container port. |
| `workers.services.metrics.enabled` | `true` | Render the worker metrics Service. |
| `workers.services.metrics.port` | `9091` | Metrics Service port. |
| `workers.services.metrics.targetPort` | `9091` | Metrics container port. |

### Migration Values

| Value | Default | Description |
| --- | --- | --- |
| `migration.enabled` | `false` | Render the migration Job. |
| `migration.targetRevision` | `head` | Alembic migration target. |
| `migration.activeDeadlineSeconds` | `3600` | Job execution deadline. |
| `migration.backoffLimit` | `0` | Job retry limit. |
| `migration.annotations` | `{}` | Annotations added to the Job. |
| `migration.resources` | See `values.yaml` | Container resource requests and limits. |
| `migration.environment.DEBUGLOG` | `"false"` | Quay migration debug logging value. |
| `migration.nodeSelector` | `{part-of: quay}` | Pod node selector. |

### Ingress Values

| Value | Default | Description |
| --- | --- | --- |
| `ingress.enabled` | `false` | Render the Ingress. |
| `ingress.className` | `""` | Ingress class name. |
| `ingress.annotations` | `{}` | Provider or controller annotations supplied by the consumer. |
| `ingress.host` | `""` | Ingress host; required when enabled. |
| `ingress.paths` | `[/]` | Prefix paths routed to Quay HTTPS. |
| `ingress.tls.enabled` | `false` | Add TLS configuration. |
| `ingress.tls.secretName` | `""` | Existing TLS Secret; required when TLS is enabled. |

## Scope Boundaries

The chart intentionally does not create or configure:

- External Secrets Operator, Vault, or Secret synchronization resources
- PostgreSQL, Redis, object storage, Clair, or other backing services
- Argo Rollouts or environment-specific deployment resources
- Environment-specific Quay configuration, credentials, hostnames, or provider settings

Supply these concerns from the deployment repository or platform that consumes the rendered chart.
