#!/usr/bin/env bash
# Prepare a release for the public repository, without publishing anything.
#
#   scripts/publish-tree.sh [base]      # base: the published branch, origin/main by default
#
# The public history is one commit per release, by a neutral author, on top of
# the previous published release. This script builds that commit on a local
# branch named "publish" from the current HEAD, leaving out what is internal to
# development, and refuses when the tree carries anything that must not be
# published. Pushing is a separate, deliberate step it prints at the end.
#
# Words that must never appear (a name, an account) are read from a file kept
# outside the repository, one per line: $AIOS_PRIVATE_WORDS, by default
# ~/.config/aios/private-words.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
base=${1:-origin/main}
version=$(cat VERSION)
author=${AIOS_PUBLISH_AUTHOR:-AIOS Development}
email=${AIOS_PUBLISH_EMAIL:-aios@localhost}
private=${AIOS_PRIVATE_WORDS:-$HOME/.config/aios/private-words}
[[ -z $(git status --porcelain) ]] || { echo 'Commit or stash the working tree first: the release is built from HEAD.' >&2; exit 1; }
git rev-parse --verify --quiet "$base" >/dev/null || { echo "Base $base not found: git fetch first." >&2; exit 1; }

work=$(mktemp -d)
tree=$work/tree
index=$work/index
mkdir "$tree"
trap 'rm -rf "$work"' EXIT
git archive HEAD | tar -x -C "$tree"
# Development notes, in the maintainers' language, are not part of the product.
rm -rf "$tree/internal"

problems=0
report() { echo "  $1" >&2; problems=1; }
echo 'Checking the tree...'
secrets='-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|github_pat[_]|gh[pousr]_[A-Za-z0-9]{30}|hf_[A-Za-z0-9]{30}|AKIA[0-9A-Z]{16}'
while read -r file; do report "secret-looking content in $file"; done < <(grep -rIlE -- "$secrets" "$tree" | sed "s|$tree/||" || true)
while read -r file; do report "personal path in $file"; done < <(grep -rIlE '/home/[a-z][a-z0-9_-]+/|/Users/[A-Za-z]' "$tree" --exclude=package-lock.json | sed "s|$tree/||" | grep -v '^installer/configure.py$' || true)
while read -r hit; do report "e-mail address: $hit"; done < <(grep -rIohE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' "$tree" --exclude=package-lock.json \
  | grep -viE '@(example\.(org|com|net)|localhost|users\.noreply\.github\.com)$|^noreply@anthropic\.com$|^git@github\.com$|\.(service|socket|timer)$' | sort -u || true)
if [[ -f $private ]]; then
  while read -r word; do
    [[ -z $word || $word == \#* ]] && continue
    while read -r file; do report "private word in $file"; done < <(grep -rIil -- "$word" "$tree" | sed "s|$tree/||" || true)
  done < "$private"
else
  echo "  (no private word list at $private: only the generic checks ran)"
fi
(( problems == 0 )) || { echo 'Nothing was prepared.' >&2; exit 1; }

GIT_INDEX_FILE=$index git --work-tree="$tree" add -A
object=$(GIT_INDEX_FILE=$index git write-tree)
commit=$(GIT_AUTHOR_NAME=$author GIT_AUTHOR_EMAIL=$email GIT_COMMITTER_NAME=$author GIT_COMMITTER_EMAIL=$email \
  git commit-tree "$object" -p "$base" -m "AIOS release $version")
git branch -f publish "$commit"
echo "Branch 'publish' holds AIOS release $version on top of $base ($(git diff --shortstat "$base" publish))."
echo "Review it with: git log -p $base..publish"
echo "Publish it, when decided, with: git push origin publish:main"
