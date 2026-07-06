#!/bin/sh
# Bootstrap an LMDB confdb from knot.conf on first start, then run knotd on
# the confdb. Dynamic configuration (conf-begin/set/commit) requires knotd
# to be running on a confdb, exactly like the production setup.
set -eu

CONFDB=/storage/confdb

if [ ! -e "$CONFDB/data.mdb" ]; then
    echo "bootstrapping confdb from /etc/knot/knot.conf"
    mkdir -p "$CONFDB"
    knotc --confdb "$CONFDB" conf-import /etc/knot/knot.conf
fi

exec knotd --confdb "$CONFDB"
