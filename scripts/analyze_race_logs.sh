#!/bin/bash
#
# Analyze race condition logs from reproduce_cosign_race.sh
#
# Usage: ./scripts/analyze_race_logs.sh [log_file]
#        ./scripts/analyze_race_logs.sh logs/quay_race_*.log

set -euo pipefail

LOG_FILE="${1:-$(ls -t logs/quay_race_*_filtered.log 2>/dev/null | head -1)}"

if [[ -z "$LOG_FILE" || ! -f "$LOG_FILE" ]]; then
    echo "Usage: $0 <log_file>"
    echo "No log file found. Run reproduce_cosign_race.sh first."
    exit 1
fi

echo "=== Race Condition Log Analysis ==="
echo "File: $LOG_FILE"
echo ""

echo "=== REPRO_TAG_RACE delay events ==="
echo ""
echo "Looking for .sig tags where both operations saw existing=False (race condition):"
echo ""

# Extract just the REPRO_TAG_RACE lines with timestamp, tag name, and existing status
grep "REPRO_TAG_RACE" "$LOG_FILE" | \
    sed -E "s/.*([0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}).*tag '([^']+)'.*existing=(True|False)/\1 | \2 | existing=\3/" | \
    sort > /tmp/race_events.txt

# Group by tag and look for races (two False in a row for same tag)
echo "Time         | Tag (truncated)                      | Status"
echo "-------------|--------------------------------------|---------------"

prev_tag=""
prev_status=""
prev_time=""

while IFS='|' read -r time tag status; do
    tag=$(echo "$tag" | xargs)  # trim whitespace
    status=$(echo "$status" | xargs)
    time=$(echo "$time" | xargs)

    # Check if this is the same tag as previous
    if [[ "$tag" == "$prev_tag" ]]; then
        # Check for race: both existing=False
        if [[ "$prev_status" == "existing=False" && "$status" == "existing=False" ]]; then
            echo ""
            echo "🐛 RACE DETECTED for tag: $tag"
            echo "   First request:  $prev_time - $prev_status"
            echo "   Second request: $time - $status"

            # Calculate time difference
            t1_ms=$(echo "$prev_time" | sed 's/://g' | sed 's/,//')
            t2_ms=$(echo "$time" | sed 's/://g' | sed 's/,//')
            echo "   Gap: ~$((t2_ms - t1_ms))ms (approximate)"
            echo ""
        fi
    fi

    # Truncate tag for display
    tag_short="${tag:0:40}"
    printf "%-12s | %-40s | %s\n" "$time" "$tag_short" "$status"

    prev_tag="$tag"
    prev_status="$status"
    prev_time="$time"
done < /tmp/race_events.txt

echo ""
echo "=== Summary ==="
total=$(grep -c "REPRO_TAG_RACE" "$LOG_FILE" 2>/dev/null || echo 0)
false_count=$(grep "REPRO_TAG_RACE" "$LOG_FILE" | grep -c "existing=False" 2>/dev/null || echo 0)
true_count=$(grep "REPRO_TAG_RACE" "$LOG_FILE" | grep -c "existing=True" 2>/dev/null || echo 0)

echo "Total REPRO_TAG_RACE events: $total"
echo "  existing=False (new tag): $false_count"
echo "  existing=True (retarget): $true_count"
echo ""
echo "Race occurs when two consecutive events for the same .sig tag both show existing=False"

rm -f /tmp/race_events.txt
