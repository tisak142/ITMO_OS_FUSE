#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 REPOSITORY BRANCH_LABEL  (requires mounted ./mnt)" >&2
  echo "Example: $0 my-test-repo fuze-demo" >&2
  exit 2
fi
repo=$1
branch=$2
mount_dir="$(dirname "$0")/mnt/$repo/branches"
if [ ! -d "$mount_dir" ]; then
  echo "Repo branches directory not mounted: $mount_dir" >&2
  exit 2
fi
label="fuse-$(date +%Y%m%d%H%M%S)"
printf 'This will create a REAL GitHub branch and commit in %s\n' "$repo"
read -r -p 'Type YES to continue: ' reply
[ "$reply" = YES ] || exit 1
mkdir "$mount_dir/$branch"
printf 'FUSE v2 smoke test\n' > "$mount_dir/$branch/workspace/$label.txt"
mkdir "$mount_dir/$branch/_commit/$label"
echo "Published SHA: $(cat "$mount_dir/$branch/_commit/$label/SHA.txt")"
echo "Verify branch and commit on GitHub."
