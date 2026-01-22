#!/bin/bash
#
# Reproduce PROJQUAY-10340: Race condition causing duplicate active tags
#
# This script uses concurrent docker push operations to trigger the race
# condition where two active tags with the same name can exist.
#
# Prerequisites:
#   - make local-dev-up running
#   - podman or docker available
#
# Usage:
#   ./scripts/reproduce_tag_race.sh [attempts]
#
# Example:
#   ./scripts/reproduce_tag_race.sh 10

set -euo pipefail

REGISTRY="localhost:8080"
ORG="admin"
REPO="race-test"
TAG="race-tag"
ATTEMPTS="${1:-5}"
DB_CONTAINER="quay-db"

# Quay registry credentials
QUAY_USER="admin"
QUAY_PASSWORD="password"

# Use podman if available, otherwise docker
if command -v podman &> /dev/null; then
    CONTAINER_CMD="podman"
else
    CONTAINER_CMD="docker"
fi

echo "=== PROJQUAY-10340 Race Condition Reproduction Script ==="
echo "Using: $CONTAINER_CMD"
echo "Registry: $REGISTRY"
echo "Repository: $ORG/$REPO"
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
$CONTAINER_CMD login "$REGISTRY" -u "$QUAY_USER" -p $QUAY_PASSWORD 2>/dev/null || {
    echo "Login failed. Make sure local-dev is running and credentials are correct."
    echo "Using: username=$QUAY_USER, password=$QUAY_PASSWORD"
    exit 1
}

# Create the test repository if it doesn't exist
echo "=== Setting up test repository ==="
# We'll just let the first push create it

# Function to create a unique image
create_test_image() {
    local image_id=$1
    local timestamp=$(date +%s%N)
    local tmpdir=$(mktemp -d)

    # Create a minimal Dockerfile with unique content
    cat > "$tmpdir/Dockerfile" <<EOF
FROM scratch
COPY data.txt /
EOF

    # Create unique data file
    echo "Image $image_id created at $timestamp - random: $RANDOM$RANDOM" > "$tmpdir/data.txt"

    # Build the image
    local image_tag="$REGISTRY/$ORG/$REPO:build-$image_id"
    $CONTAINER_CMD build -t "$image_tag" "$tmpdir" -q > /dev/null 2>&1

    rm -rf "$tmpdir"
    echo "$image_tag"
}

# Function to push and immediately retag
push_image() {
    local source_tag=$1
    local target_tag=$2
    local log_file=$3

    # Tag to the target
    $CONTAINER_CMD tag "$source_tag" "$target_tag" 2>/dev/null

    # Push (this will create/retarget the tag)
    $CONTAINER_CMD push "$target_tag" > "$log_file" 2>&1
    echo $?
}

# Function to check for duplicate active tags
check_for_duplicates() {
    local tag_name=$1

    # Query the database for active tags with this name
    local result=$($CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -t -A -c "
        SELECT COUNT(*)
        FROM tag t
        JOIN repository r ON t.repository_id = r.id
        JOIN \"user\" u ON r.namespace_user_id = u.id
        WHERE u.username = '$ORG'
          AND r.name = '$REPO'
          AND t.name = '$tag_name'
          AND t.lifetime_end_ms IS NULL;
    " 2>/dev/null | tr -d '[:space:]')

    echo "$result"
}

# Function to get tag details
get_tag_details() {
    local tag_name=$1

    $CONTAINER_CMD exec $DB_CONTAINER psql -U quay -d quay -t -A -c "
        SELECT t.id, t.manifest_id, t.lifetime_start_ms, t.lifetime_end_ms
        FROM tag t
        JOIN repository r ON t.repository_id = r.id
        JOIN \"user\" u ON r.namespace_user_id = u.id
        WHERE u.username = '$ORG'
          AND r.name = '$REPO'
          AND t.name = '$tag_name'
        ORDER BY t.lifetime_start_ms;
    " 2>/dev/null
}

echo ""
echo "=== Creating test images ==="

# Pre-create images to avoid build time affecting the race
IMAGE_A=$(create_test_image "a")
IMAGE_B=$(create_test_image "b")
echo "Image A: $IMAGE_A"
echo "Image B: $IMAGE_B"

# Push images once to ensure manifests exist in the registry
echo ""
echo "=== Initial push to populate manifests ==="
$CONTAINER_CMD push "$IMAGE_A" > /dev/null 2>&1
$CONTAINER_CMD push "$IMAGE_B" > /dev/null 2>&1
echo "Done."

RACE_DETECTED=0
SUCCESSFUL_REPRODUCTIONS=0

echo ""
echo "=== Starting race condition attempts ==="

for attempt in $(seq 1 $ATTEMPTS); do
    # Use a unique tag name for each attempt
    TEST_TAG="${TAG}-${attempt}-$(date +%s)"
    TARGET="$REGISTRY/$ORG/$REPO:$TEST_TAG"

    echo ""
    echo "--- Attempt $attempt/$ATTEMPTS (tag: $TEST_TAG) ---"

    # Create temp files for logging
    LOG_A=$(mktemp)
    LOG_B=$(mktemp)

    # Tag both images to the same target
    $CONTAINER_CMD tag "$IMAGE_A" "$TARGET" 2>/dev/null

    # Push both images concurrently to the SAME tag
    # This is the key - both pushes try to create/update the same tag simultaneously
    (
        $CONTAINER_CMD push "$TARGET" > "$LOG_A" 2>&1
    ) &
    PID_A=$!

    # Immediately re-tag and push the second image
    $CONTAINER_CMD tag "$IMAGE_B" "$TARGET" 2>/dev/null
    (
        $CONTAINER_CMD push "$TARGET" > "$LOG_B" 2>&1
    ) &
    PID_B=$!

    # Wait for both pushes to complete
    wait $PID_A 2>/dev/null || true
    wait $PID_B 2>/dev/null || true

    # Small delay to let DB settle
    sleep 0.5

    # Check for duplicate active tags
    ACTIVE_COUNT=$(check_for_duplicates "$TEST_TAG")

    if [[ "$ACTIVE_COUNT" -gt 1 ]]; then
        echo "🐛 BUG REPRODUCED! Found $ACTIVE_COUNT active tags with name '$TEST_TAG'"
        echo ""
        echo "Tag details:"
        get_tag_details "$TEST_TAG"
        echo ""
        RACE_DETECTED=1
        ((SUCCESSFUL_REPRODUCTIONS++))
    else
        echo "✓ No race detected (1 active tag)"
    fi

    # Cleanup temp files
    rm -f "$LOG_A" "$LOG_B"
done

echo ""
echo "=== Summary ==="
echo "Total attempts: $ATTEMPTS"
echo "Successful reproductions: $SUCCESSFUL_REPRODUCTIONS"

if [[ $RACE_DETECTED -eq 1 ]]; then
    echo ""
    echo "🐛 RACE CONDITION CONFIRMED!"
    echo ""
    echo "The bug occurs because:"
    echo "1. Two concurrent pushes both read 'no existing tag' (or same existing tag)"
    echo "2. Both create new tag entries with lifetime_end_ms = NULL"
    echo "3. PostgreSQL's unique constraint allows multiple NULLs"
    echo ""
    echo "Fix: Add partial unique index:"
    echo "  CREATE UNIQUE INDEX tag_repository_name_active_unique"
    echo "  ON tag (repository_id, name)"
    echo "  WHERE lifetime_end_ms IS NULL;"
    exit 0
else
    echo ""
    echo "Race condition not triggered in $ATTEMPTS attempts."
    echo "This is a timing-dependent bug. Try:"
    echo "  1. Increase attempts: $0 50"
    echo "  2. Add artificial delay in retarget_tag() between get_tag() and Tag.create()"
    echo "  3. Run under higher system load"
    exit 1
fi
