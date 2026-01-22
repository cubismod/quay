#!/bin/bash
#
# Reproduce PROJQUAY-10340: Race condition with concurrent cosign operations
#
# Simulates the customer's release pipeline where two tasks write to .sig:
#   1. push-snapshot: cosign copy (copies image + .sig from build to prod repo)
#   2. rh-sign-image-cosign: cosign sign (signs image in prod repo)
#
# Prerequisites:
#   - make local-dev-up running
#   - cosign installed (https://docs.sigstore.dev/cosign/installation/)
#   - podman or docker available
#
# Usage:
#   ./scripts/reproduce_cosign_race.sh [attempts]

set -euo pipefail

REGISTRY="localhost:8080"
BUILD_ORG="admin"
BUILD_REPO="build-repo"
PROD_ORG="admin"
PROD_REPO="prod-repo"
ATTEMPTS="${1:-5}"
DB_CONTAINER="quay-db"
QUAY_CONTAINER="quay-quay"

# Quay registry credentials
QUAY_USER="admin"
QUAY_PASSWORD="password"

# Log capture setup
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="./logs"
QUAY_LOG_FILE="${LOG_DIR}/quay_race_${TIMESTAMP}.log"
LOG_PID=""

mkdir -p "$LOG_DIR"

start_log_capture() {
    echo "Starting Quay log capture -> $QUAY_LOG_FILE"
    # Use --since to only capture new logs from this point forward
    docker logs -f --since "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$QUAY_CONTAINER" > "$QUAY_LOG_FILE" 2>&1 &
    LOG_PID=$!
}

stop_log_capture() {
    if [[ -n "$LOG_PID" ]]; then
        kill "$LOG_PID" 2>/dev/null || true
        wait "$LOG_PID" 2>/dev/null || true
        echo "Quay logs saved to: $QUAY_LOG_FILE"

        # Extract just the race-related logs
        RACE_LOG_FILE="${LOG_DIR}/quay_race_${TIMESTAMP}_filtered.log"
        grep -E "(REPRO_TAG_RACE|retarget_tag|\.sig)" "$QUAY_LOG_FILE" > "$RACE_LOG_FILE" 2>/dev/null || true
        if [[ -s "$RACE_LOG_FILE" ]]; then
            echo "Filtered race logs: $RACE_LOG_FILE"
        fi
    fi
}

trap stop_log_capture EXIT

# Use podman if available, otherwise docker
if command -v podman &> /dev/null; then
    CONTAINER_CMD="podman"
else
    CONTAINER_CMD="docker"
fi

# Check for cosign
if ! command -v cosign &> /dev/null; then
    echo "ERROR: cosign is not installed"
    echo "Install from: https://docs.sigstore.dev/cosign/installation/"
    exit 1
fi

echo "=== PROJQUAY-10340 Cosign Race Condition Reproduction ==="
echo "Simulating customer pipeline:"
echo "  1. push-snapshot: cosign copy (build -> prod)"
echo "  2. rh-sign-image-cosign: cosign sign (prod)"
echo ""
echo "Registry: $REGISTRY"
echo "Build repo: $BUILD_ORG/$BUILD_REPO"
echo "Prod repo: $PROD_ORG/$PROD_REPO"
echo "Attempts: $ATTEMPTS"
echo ""

# Check if local-dev is running
if ! curl -s -o /dev/null -w '' "http://$REGISTRY/health" 2>/dev/null; then
    echo "ERROR: Quay doesn't appear to be running at $REGISTRY"
    echo "Start it with: make local-dev-up"
    exit 1
fi

# Login to local registry
echo "=== Logging into local registry ==="
$CONTAINER_CMD login "$REGISTRY" -u "$QUAY_USER" -p "$QUAY_PASSWORD"  2>/dev/null || {
    echo "Login failed."
    exit 1
}

# Also login for cosign
echo "$QUAY_PASSWORD" | cosign login "$REGISTRY" -u "$QUAY_USER" --password-stdin 2>/dev/null || true

# Generate a cosign key pair for signing (if not exists)
if [[ ! -f cosign.key ]]; then
    echo "=== Generating cosign key pair ==="
    COSIGN_PASSWORD="" cosign generate-key-pair 2>/dev/null
fi

# Function to create and push a test image
create_and_push_image() {
    local org=$1
    local repo=$2
    local tag=$3
    local tmpdir=$(mktemp -d)

    cat > "$tmpdir/Dockerfile" <<EOF
FROM scratch
COPY data.txt /
EOF
    echo "Image created at $(date +%s%N) - $RANDOM" > "$tmpdir/data.txt"

    local image="$REGISTRY/$org/$repo:$tag"
    $CONTAINER_CMD build -t "$image" "$tmpdir" -q > /dev/null 2>&1
    $CONTAINER_CMD push "$image"  > /dev/null 2>&1

    rm -rf "$tmpdir"
    echo "$image"
}

# Function to check for duplicate active tags
check_for_duplicates() {
    local org=$1
    local repo=$2
    local tag_pattern=$3

    local result=$($CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -t -A -c "
        SELECT COUNT(*)
        FROM tag t
        JOIN repository r ON t.repository_id = r.id
        JOIN \"user\" u ON r.namespace_user_id = u.id
        WHERE u.username = '$org'
          AND r.name = '$repo'
          AND t.name LIKE '$tag_pattern'
          AND t.lifetime_end_ms IS NULL;
    " 2>/dev/null | tr -d '[:space:]')

    echo "$result"
}

# Function to get tag details
get_sig_tags() {
    local org=$1
    local repo=$2

    $CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -c "
        SELECT t.id, t.name, t.manifest_id, t.lifetime_start_ms,
               CASE WHEN t.lifetime_end_ms IS NULL THEN 'ACTIVE' ELSE 'expired' END as status
        FROM tag t
        JOIN repository r ON t.repository_id = r.id
        JOIN \"user\" u ON r.namespace_user_id = u.id
        WHERE u.username = '$org'
          AND r.name = '$repo'
          AND t.name LIKE '%.sig'
        ORDER BY t.name, t.lifetime_start_ms;
    " 2>/dev/null
}

RACE_DETECTED=0
SUCCESSFUL_REPRODUCTIONS=0

# Start capturing Quay logs
start_log_capture

echo ""
echo "=== Starting race condition attempts ==="

for attempt in $(seq 1 $ATTEMPTS); do
    echo ""
    echo "--- Attempt $attempt/$ATTEMPTS ---"

    # Create unique tag for this attempt
    TAG="test-image-$attempt-$(date +%s)"
    SIG_TAG="sha256-*.sig"  # Pattern for .sig tags

    # Step 1: Create and push image to BUILD repo, then sign it
    echo "Creating image in build repo..."
    BUILD_IMAGE=$(create_and_push_image "$BUILD_ORG" "$BUILD_REPO" "$TAG")

    # Get the digest (strip any newlines/whitespace)
    DIGEST=$($CONTAINER_CMD image inspect "$BUILD_IMAGE" --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2 | tr -d '[:space:]')

    if [[ -z "$DIGEST" || ! "$DIGEST" =~ ^sha256: ]]; then
        echo "Could not get image digest, skipping..."
        continue
    fi

    BUILD_IMAGE_DIGEST="${REGISTRY}/${BUILD_ORG}/${BUILD_REPO}@${DIGEST}"
    PROD_IMAGE_DIGEST="${REGISTRY}/${PROD_ORG}/${PROD_REPO}@${DIGEST}"
    PROD_IMAGE_TAG="$REGISTRY/$PROD_ORG/$PROD_REPO:$TAG"

    echo "Build image: $BUILD_IMAGE_DIGEST"

    # Sign the image in build repo first
    echo "Signing image in build repo..."
    COSIGN_PASSWORD="" cosign sign --key cosign.key  -y "$BUILD_IMAGE_DIGEST" 2>/dev/null || true

    # Step 2: Simulate the race condition
    # Task 1 (push-snapshot): cosign copy from build to prod
    # Task 2 (rh-sign-image-cosign): cosign sign in prod

    echo "Simulating concurrent pipeline tasks..."

    LOG_COPY=$(mktemp)
    LOG_SIGN=$(mktemp)

    # First, copy the image (without signature) to prod so both tasks have something to work with
    $CONTAINER_CMD tag "$BUILD_IMAGE" "$PROD_IMAGE_TAG" 2>/dev/null
    $CONTAINER_CMD push "$PROD_IMAGE_TAG"  > /dev/null 2>&1

    # Now run both cosign operations concurrently
    (
        # Task 1: cosign copy (copies image + .sig from build to prod)
        cosign copy --force "$BUILD_IMAGE_DIGEST" "$PROD_IMAGE_DIGEST" --allow-insecure-registry 2>&1 > "$LOG_COPY"
    ) &
    PID_COPY=$!

    (
        # Task 2: cosign sign (creates new .sig in prod)
        COSIGN_PASSWORD="" cosign sign --key cosign.key  -y "$PROD_IMAGE_DIGEST" 2>&1 > "$LOG_SIGN"
    ) &
    PID_SIGN=$!

    # Wait for both
    wait $PID_COPY 2>/dev/null || true
    wait $PID_SIGN 2>/dev/null || true

    sleep 0.5

    # Check for duplicate .sig tags
    # The .sig tag name is based on the image digest (sha256:abc... -> sha256-abc....sig)
    SIG_TAG_NAME=$(echo "${DIGEST}" | tr ':' '-').sig

    ACTIVE_COUNT=$($CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -t -A -c "
        SELECT COUNT(*)
        FROM tag t
        JOIN repository r ON t.repository_id = r.id
        JOIN \"user\" u ON r.namespace_user_id = u.id
        WHERE u.username = '$PROD_ORG'
          AND r.name = '$PROD_REPO'
          AND t.name = '$SIG_TAG_NAME'
          AND t.lifetime_end_ms IS NULL;
    " 2>/dev/null | tr -d '[:space:]')

    if [[ "$ACTIVE_COUNT" -gt 1 ]]; then
        echo "🐛 BUG REPRODUCED! Found $ACTIVE_COUNT active .sig tags"
        echo ""
        echo "Tag details for $SIG_TAG_NAME:"
        $CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -c "
            SELECT t.id, t.manifest_id, t.lifetime_start_ms, t.lifetime_end_ms
            FROM tag t
            JOIN repository r ON t.repository_id = r.id
            JOIN \"user\" u ON r.namespace_user_id = u.id
            WHERE u.username = '$PROD_ORG'
              AND r.name = '$PROD_REPO'
              AND t.name = '$SIG_TAG_NAME'
            ORDER BY t.lifetime_start_ms;
        " 2>/dev/null
        RACE_DETECTED=1
        ((SUCCESSFUL_REPRODUCTIONS++))
    else
        echo "✓ No race detected (${ACTIVE_COUNT:-0} active .sig tag)"
    fi

    rm -f "$LOG_COPY" "$LOG_SIGN"
done

echo ""
echo "=== Summary ==="
echo "Total attempts: $ATTEMPTS"
echo "Successful reproductions: $SUCCESSFUL_REPRODUCTIONS"

if [[ $RACE_DETECTED -eq 1 ]]; then
    echo ""
    echo "🐛 RACE CONDITION CONFIRMED!"
    echo ""
    echo "The customer's pipeline triggers this because:"
    echo "  1. push-snapshot runs 'cosign copy' (copies .sig from build repo)"
    echo "  2. rh-sign-image-cosign runs 'cosign sign' (creates new .sig)"
    echo "  Both write to the same .sig tag concurrently."
    echo ""
    echo "Fix: Add partial unique index:"
    echo "  CREATE UNIQUE INDEX tag_repository_name_active_unique"
    echo "  ON tag (repository_id, name)"
    echo "  WHERE lifetime_end_ms IS NULL;"
else
    echo ""
    echo "Race condition not triggered in $ATTEMPTS attempts."
    echo "Try increasing attempts or adding artificial delay in retarget_tag()"
fi

# Cleanup
rm -f cosign.key cosign.pub
