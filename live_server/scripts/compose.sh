#!/bin/sh
# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname -- "$script_dir")
repo_dir=$(git -C "$project_dir" rev-parse --show-toplevel)

source_revision=${SOURCE_REVISION:-$(git -C "$repo_dir" rev-parse HEAD)}
source_repository_url=${SOURCE_REPOSITORY_URL:-$(git -C "$repo_dir" remote get-url origin)}

# Convert common GitHub SSH remotes to public browser URLs. HTTPS remotes work
# as-is; deployments using another URL shape can set SOURCE_CODE_URL directly.
case "$source_repository_url" in
  git@github.com:*)
    source_repository_url="https://github.com/${source_repository_url#git@github.com:}"
    ;;
  ssh://git@github.com/*)
    source_repository_url="https://github.com/${source_repository_url#ssh://git@github.com/}"
    ;;
esac
source_repository_url=${source_repository_url%.git}

if [ "${ENVIRONMENT:-development}" = "production" ] &&
   [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=normal)" ]; then
  echo "Refusing a production deployment from a dirty worktree." >&2
  echo "Commit the deployed changes so the source link identifies the exact code." >&2
  exit 1
fi

SOURCE_REPOSITORY_URL=$source_repository_url
SOURCE_REVISION=$source_revision
SOURCE_CODE_URL=${SOURCE_CODE_URL:-"$source_repository_url/tree/$source_revision"}
export SOURCE_REPOSITORY_URL SOURCE_REVISION SOURCE_CODE_URL

exec docker compose -f "$project_dir/docker-compose.yml" "$@"
