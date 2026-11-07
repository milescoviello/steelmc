#!/bin/sh
# Python leaves __pycache__ folders when a tool runs as root; clear them so removing the package leaves nothing behind.
find /usr/lib/steelmc -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
