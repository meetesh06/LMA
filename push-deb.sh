#!/usr/bin/env bash
# Build a signed LMA source package and upload it to the Launchpad PPA.
#   ./push-deb.sh                 checks, build, confirm, upload
#   ./push-deb.sh --build-only    checks and build, no upload
#   ./push-deb.sh --since 1.0.3-0ubuntu1   include changelog entries after this version
#                                          (default: the newest version already in the PPA)
#   ./push-deb.sh --key KEYID     sign with this GPG key (default: the changelog maintainer's)
#   ./push-deb.sh --ppa ppa:user/name     (default: ppa:meetesh06/radio-banana)
#   ./push-deb.sh -y              do not ask before uploading
# Bump the version first with:  (cd lma-src && dch -i)  and update LMA_VERSION in lmalib.py.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC="$ROOT/lma-src"
PPA="ppa:meetesh06/radio-banana"
SINCE="" KEY="" BUILD_ONLY=0 YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --build-only) BUILD_ONLY=1 ;;
    --since) SINCE=${2:?--since needs a version}; shift ;;
    --key) KEY=${2:?--key needs a key id}; shift ;;
    --ppa) PPA=${2:?--ppa needs ppa:user/name}; shift ;;
    -y|--yes) YES=1 ;;
    -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

die()  { echo "!! $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
ask()  { [ $YES -eq 1 ] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

# --- 1. Tools ---------------------------------------------------------------
step "Checking tools"
missing=()
for t in debuild dput gpg dpkg-parsechangelog curl python3; do command -v "$t" >/dev/null || missing+=("$t"); done
[ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]}  (sudo apt install devscripts dput gnupg curl)"
echo "ok"

# --- 2. Version consistency -------------------------------------------------
step "Reading debian/changelog"
cl() { dpkg-parsechangelog -l "$SRC/debian/changelog" -S "$1"; }
VERSION=$(cl Version); DIST=$(cl Distribution); MAINT=$(cl Maintainer)
EMAIL=$(sed -n 's/.*<\(.*\)>.*/\1/p' <<<"$MAINT")
UPSTREAM=${VERSION%%-*}
echo "version $VERSION  ->  $DIST   (maintainer $MAINT)"
[ "$DIST" != "UNRELEASED" ] || die "changelog distribution is UNRELEASED; set it (e.g. jammy) with: dch -r"
CODE_VERSION=$(sed -n 's/^LMA_VERSION = "\(.*\)".*/\1/p' "$SRC/src/lib/lmalib.py")
[ "$CODE_VERSION" = "$UPSTREAM" ] || die "lmalib.py LMA_VERSION is \"$CODE_VERSION\" but the changelog says $UPSTREAM; make them match"

# --- 3. Signing key ---------------------------------------------------------
step "Checking GPG key"
if [ -n "$KEY" ]; then
  gpg --list-secret-keys "$KEY" >/dev/null 2>&1 || die "no secret key $KEY"
  echo "using key $KEY"
else
  gpg --list-secret-keys "$EMAIL" >/dev/null 2>&1 \
    || die "no secret GPG key for $EMAIL (the changelog maintainer); pass --key or fix the changelog email"
  echo "found a key for $EMAIL (it must be registered on your Launchpad account)"
fi

# --- 4. What the PPA already has -------------------------------------------
step "Asking Launchpad which versions $PPA already has"
OWNER=${PPA#ppa:}; OWNER=${OWNER%%/*}; NAME=${PPA#*/}
API="https://api.launchpad.net/1.0/~$OWNER/+archive/ubuntu/$NAME?ws.op=getPublishedSources&source_name=lma&exact_match=true"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fetch() { local i; for i in 1 2 3; do curl -fsSL --max-time 60 -o "$2" "$1" 2>/dev/null && return 0; sleep 2; done; return 1; }
# "version<TAB>self_link" per published source
PUBS=""
if fetch "$API" "$TMP/pubs.json"; then
  PUBS=$(python3 -I -c 'import json,sys
for e in json.load(open(sys.argv[1]))["entries"]:
    print(e["source_package_version"] + "\t" + e["self_link"])' "$TMP/pubs.json" 2>/dev/null) || PUBS=""
fi
PUBLISHED=$(cut -f1 <<<"$PUBS" | sed '/^$/d' | sort -uV)
if [ -z "$PUBLISHED" ]; then
  echo "could not read the PPA (offline, or nothing published yet); skipping the version check"
else
  NEWEST=$(sort -V <<<"$PUBLISHED" | tail -1)
  echo "published: $(tr '\n' ' ' <<<"$PUBLISHED")"
  grep -qx "$VERSION" <<<"$PUBLISHED" && die "$VERSION is already in the PPA; Launchpad never accepts a version twice. Bump with: (cd lma-src && dch -i)"
  dpkg --compare-versions "$VERSION" gt "$NEWEST" || die "$VERSION is not newer than $NEWEST in the PPA; bump with: (cd lma-src && dch -i)"
  [ -n "$SINCE" ] || SINCE=$NEWEST
fi

# --- 5. Working tree --------------------------------------------------------
if command -v git >/dev/null && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(git -C "$ROOT" status --porcelain -- lma-src ':!lma-src/debian/lma' ':!lma-src/debian/.debhelper')" ]; then
    echo; echo "Note: lma-src has uncommitted changes; the upload will contain them as they are now."
    ask "Continue anyway?" || exit 1
  fi
fi

# --- 6. Build ---------------------------------------------------------------
step "Cleaning old build output"
"$ROOT/clean.sh"

# Source format 1.0 with a Debian revision needs lma_<upstream>.orig.tar.gz (src/ only,
# like earlier uploads). A re-upload of the same upstream version (e.g. -0ubuntu2) must reuse
# the orig tarball already in the PPA byte for byte, so download it instead of making a new one.
ORIG="$ROOT/lma_${UPSTREAM}.orig.tar.gz"
SAME_UPSTREAM=$(awk -F'\t' -v u="$UPSTREAM" 'index($1, u "-") == 1 {print $2; exit}' <<<"$PUBS")
if [ -n "$SAME_UPSTREAM" ]; then
  step "Reusing the published lma_${UPSTREAM}.orig.tar.gz"
  fetch "$SAME_UPSTREAM?ws.op=sourceFileUrls" "$TMP/urls.json" || die "could not list the published source files"
  url=$(python3 -I -c 'import json,sys; print(next(u for u in json.load(open(sys.argv[1])) if u.endswith(".orig.tar.gz")))' "$TMP/urls.json") \
    || die "the published upload has no orig tarball"
  fetch "$url" "$ORIG" || die "could not download $url"
  SOURCE_OPT=-sd     # orig already on Launchpad: upload only the diff
else
  step "Creating lma_${UPSTREAM}.orig.tar.gz"
  tar -C "$SRC" --exclude=__pycache__ --owner=0 --group=0 -czf "$ORIG" ./src
  tar -tzf "$ORIG" | sed 's/^/  /'
  SOURCE_OPT=-sa     # new upstream version: include the orig tarball
fi

step "Building signed source package"
args=(-S "$SOURCE_OPT")
[ -n "$SINCE" ] && args+=("-v$SINCE") && echo "including changelog entries after $SINCE"
[ -n "$KEY" ] && args+=("-k$KEY")
(cd "$SRC" && debuild "${args[@]}")

CHANGES="$ROOT/lma_${VERSION}_source.changes"
[ -f "$CHANGES" ] || die "expected $CHANGES was not created"
echo "built $(basename "$CHANGES")"

if [ $BUILD_ONLY -eq 1 ]; then
  echo; echo "Build only: upload later with:  dput $PPA $CHANGES"
  exit 0
fi

# --- 7. Upload --------------------------------------------------------------
echo
ask "Upload lma $VERSION to $PPA ($DIST)? This cannot be undone or re-used." || { echo "Not uploaded."; exit 0; }
step "Uploading"
dput "$PPA" "$CHANGES"

echo
echo "Uploaded. Launchpad emails you when it is accepted, then builds it."
echo "Progress: https://launchpad.net/~$OWNER/+archive/ubuntu/$NAME/+packages"
echo "Then on the servers:  sudo apt update && sudo apt install --only-upgrade lma"
