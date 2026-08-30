#!/bin/bash

set -eu
umask 077

launcher_path="$0"
case "$launcher_path" in
    /*) ;;
    *) launcher_path="$PWD/$launcher_path" ;;
esac
repository_root="${launcher_path%/*}/.."
source_file="$repository_root/scripts/volcengine_agent_plan_launcher.m"
build_directory="$(mktemp -d /private/tmp/search-agent-launcher.XXXXXX)"
launcher_binary="$build_directory/launcher"

cleanup_build() {
    if [ -f "$launcher_binary" ]; then
        rm -f "$launcher_binary"
    fi
    if [ -d "$build_directory" ]; then
        rmdir "$build_directory" 2>/dev/null || true
    fi
}
trap cleanup_build EXIT HUP INT TERM

/usr/bin/clang -O2 -fobjc-arc \
    -framework AppKit -framework Foundation \
    "$source_file" -o "$launcher_binary"
chmod 0700 "$launcher_binary"
"$launcher_binary" "$repository_root"
