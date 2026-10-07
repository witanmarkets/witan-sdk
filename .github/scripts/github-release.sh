#!/usr/bin/env bash
# The GitHub Release for the tag this run is on; publish.yml runs it once the package is out.
# Its notes are the tag's CHANGELOG.md entry: everything under the "## X.Y.Z — <date>" heading up to
# the next heading, as every release here has had. It is Latest only when the tag is the highest
# version on the repository, so a patch to an older line does not take that. Run again for the same
# tag, it edits the release instead of failing.
# Needs GITHUB_REF_NAME (vX.Y.Z), GH_TOKEN and GH_REPO for gh, and an `origin` to read the tags from.
# The same file is in both SDK mirrors (sdk/python, sdk/js); scripts/test-release-sdk.sh keeps them
# the same and runs it.
set -euo pipefail
TAG=${GITHUB_REF_NAME:?}
V=${TAG#v}
NOTES=$(mktemp)
trap 'rm -f "$NOTES"' EXIT
awk -v v="$V" '/^## /{ if (f) exit; if ($2 == v) { f = 1; next } } f' CHANGELOG.md > "$NOTES"
grep -q '[^[:space:]]' "$NOTES" || { echo "::error::CHANGELOG.md has no entry for $V"; exit 1; }
TOP=$(git ls-remote --tags origin | sed -n 's#.*refs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$#\1#p' | sort -V | tail -1)
LATEST=false
[ "$TAG" = "$TOP" ] && LATEST=true
if gh release view "$TAG" > /dev/null 2>&1; then
  gh release edit "$TAG" --title "witan-sdk $V" --notes-file "$NOTES" --latest="$LATEST"
  echo "updated the release $TAG (latest: $LATEST)"
else
  gh release create "$TAG" --verify-tag --title "witan-sdk $V" --notes-file "$NOTES" --latest="$LATEST"
  echo "created the release $TAG (latest: $LATEST)"
fi
