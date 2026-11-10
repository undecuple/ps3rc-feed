#!/usr/bin/env python3
"""Copy feed.json and manifest.json from a feed-state checkout over a site data directory.

Usage: overlay_feed.py [--required] [--expect-version HEX] SOURCE_DIR DEST_DIR

The files are copied only when both are regular files, both parse as JSON
objects, the manifest names feed.json, its version is the first 16 hex digits
of the SHA-256 of feed.json, and both carry the same generated_at. Otherwise
DEST_DIR is left as it is. Without --required that is exit 0 (keep the
committed data); with --required it is exit 1. Nothing from the files is
printed except the version.
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
    return feed_bytes, manifest_bytes, version


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
