#!/usr/bin/env python3
"""Copy feed.json and manifest.json from a feed-state checkout over a site data directory.

Usage: overlay_feed.py [--required] [--expect-version HEX] SOURCE_DIR DEST_DIR

The files are copied only when both are regular files, both parse as JSON
objects, the manifest names feed.json, its version is the first 16 hex digits
of the SHA-256 of feed.json, both carry the same generated_at, and the feed
is fit to be served: its repos list is not empty, its collection object
(when it has one) says stopped null or "budget", and when DEST_DIR already
holds a feed.json, the new repos list is at least half as long as that one
(a feed.json in DEST_DIR that cannot be read is a refusal). Otherwise
DEST_DIR is left as it is. Without --required that is exit 0 (keep the
committed data); with --required it is exit 1. Nothing from the files is
printed except the version and repository counts.
"""

import hashlib
import json
import os
import stat
import sys

FEED = "feed.json"
MANIFEST = "manifest.json"
VERSION_HEX = 16
MAX_BYTES = 16 * 1024 * 1024
HEX = frozenset("0123456789abcdef")
# Stop reasons of a run whose feed may replace the served one: none, or the normal budget stop.
PUBLISHABLE_STOPS = (None, "budget")


class Mismatch(Exception):
    pass


def _read_regular(path):
    try:
        st = os.lstat(path)
    except OSError:
        raise Mismatch("%s is missing" % os.path.basename(path)) from None
    if not stat.S_ISREG(st.st_mode):
        raise Mismatch("%s is not a regular file" % os.path.basename(path))
    if st.st_size > MAX_BYTES:
        raise Mismatch("%s is too large" % os.path.basename(path))
    with open(path, "rb") as handle:
        return handle.read()


def _load_object(data, name):
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError:
        raise Mismatch("%s does not parse" % name) from None
    if not isinstance(value, dict):
        raise Mismatch("%s is not a JSON object" % name)
    return value


def check(source):
    """Return (feed_bytes, manifest_bytes, version) or raise Mismatch with a reason that holds no file content."""
    feed_bytes = _read_regular(os.path.join(source, FEED))
    manifest_bytes = _read_regular(os.path.join(source, MANIFEST))
    manifest = _load_object(manifest_bytes, MANIFEST)
    feed = _load_object(feed_bytes, FEED)
    version = manifest.get("version")
    if not (isinstance(version, str) and len(version) == VERSION_HEX and set(version) <= HEX):
        raise Mismatch("manifest version is not %d lowercase hex digits" % VERSION_HEX)
    if manifest.get("feed") != FEED:
        raise Mismatch("manifest does not name feed.json")
    if hashlib.sha256(feed_bytes).hexdigest()[:VERSION_HEX] != version:
        raise Mismatch("feed.json hash does not match the manifest version")
    if not isinstance(feed.get("generated_at"), str) or manifest.get("generated_at") != feed.get("generated_at"):
        raise Mismatch("manifest and feed generated_at differ")
    repos = _repo_count(feed, FEED)
    if repos == 0:
        raise Mismatch("feed.json lists no repositories")
    # Feeds written before the collection object existed (the committed site data) have none: not a refusal.
    if "collection" in feed:
        collection = feed["collection"]
        if not isinstance(collection, dict):
            raise Mismatch("feed.json collection is not an object")
        if collection.get("stopped") not in PUBLISHABLE_STOPS:
            raise Mismatch("feed.json comes from a run that stopped abnormally")
    return feed_bytes, manifest_bytes, version


def _repo_count(feed, name):
    repos = feed.get("repos")
    if not isinstance(repos, list):
        raise Mismatch("%s has no repos list" % name)
    return len(repos)


def check_replaces(feed_bytes, dest):
    """Raise Mismatch when DEST_DIR's feed.json lists more than twice the repositories of the new feed."""
    target = os.path.join(dest, FEED)
    if not os.path.lexists(target):
        return
    current = _repo_count(_load_object(_read_regular(target), "the feed.json being replaced"), "the feed.json being replaced")
    new = _repo_count(_load_object(feed_bytes, FEED), FEED)
    if new * 2 < current:
        raise Mismatch("feed.json lists %d repositories, under half of the %d it would replace" % (new, current))


def _replace(dest, name, data):
    target = os.path.join(dest, name)
    temp = target + ".tmp"
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, target)


def main(argv):
    required = False
    expect = None
    args = list(argv)
    if args and args[0] == "--required":
        required = True
        args.pop(0)
    if len(args) >= 2 and args[0] == "--expect-version":
        expect = args[1]
        args = args[2:]
    if len(args) != 2:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    source, dest = args
    try:
        feed_bytes, manifest_bytes, version = check(source)
        if expect is not None and version != expect:
            raise Mismatch("manifest version is not the expected one")
        if not os.path.isdir(dest) or os.path.islink(dest):
            raise Mismatch("destination is not a directory")
        check_replaces(feed_bytes, dest)
    except Mismatch as exc:
        print("overlay: kept the committed data (%s)" % exc)
        return 1 if required else 0
    # The feed first: a page that sees the new manifest version then finds the matching feed.
    _replace(dest, FEED, feed_bytes)
    _replace(dest, MANIFEST, manifest_bytes)
    print("overlay: feed version %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
