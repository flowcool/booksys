#!/usr/bin/env bash
# Read-only trusted-release gate. Run again immediately before public writes.
set -euo pipefail
destination=${1:?usage: release_preflight.sh DIRECTORY}
mkdir -p "$destination"
[[ "$GITHUB_EVENT_NAME" == release && "$GITHUB_REPOSITORY" == flowcool/booksys ]]
tag=$(python3 -c 'import json,os; print(json.load(open(os.environ["GITHUB_EVENT_PATH"]))["release"]["tag_name"])')
[[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
gh api "repos/flowcool/booksys/releases/tags/$tag" > "$destination/remote-release.json"
python3 - "$destination/remote-release.json" <<'PY'
import json,os,sys
event=json.load(open(os.environ['GITHUB_EVENT_PATH']))
remote=json.load(open(sys.argv[1]))
for key in ('id','tag_name','draft','prerelease','published_at'):
    if remote[key] != event['release'][key]:
        raise SystemExit('Release control plane changed: '+key)
PY
if [[ "$(git rev-parse --is-shallow-repository)" == true ]]; then
  git fetch --unshallow --no-tags origin
fi
git fetch --no-tags origin main "refs/tags/$tag:refs/tags/$tag"
revision=$(git rev-parse "refs/tags/$tag^{commit}")
[[ "$revision" == "$GITHUB_SHA" && "$(git rev-parse HEAD)" == "$revision" ]]
# A later main commit does not invalidate an approved ancestor release.
ancestor=$(git merge-base "$revision" origin/main)
latest_tag=
prerelease=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["prerelease"])' "$destination/remote-release.json")
if [[ "$prerelease" == False ]]; then
  # A stable event is not permission to downgrade latest. GitHub must designate it latest.
  gh api repos/flowcool/booksys/releases/latest > "$destination/latest-release.json"
  latest_tag=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tag_name"])' "$destination/latest-release.json")
fi
python3 ops/release_images.py event "$GITHUB_EVENT_PATH" --event-name "$GITHUB_EVENT_NAME" \
  --ref "$GITHUB_REF" --revision "$revision" --main "$ancestor" --latest-tag "$latest_tag" > "$destination/release.json"
