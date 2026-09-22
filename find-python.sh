# Sourced, not run. Sets PYTHON to a python3 that is 3.11 or newer.
#
# Not just `python3` on PATH: on a stock Mac that is Apple's 3.9, which the
# store cannot run on, and under launchd PATH is nearly empty anyway. Homebrew
# first because that is where a newer one usually is; $CSM_PYTHON wins.
find_python() {
    local candidate
    for candidate in "${CSM_PYTHON:-}" /opt/homebrew/bin/python3 \
                     /usr/local/bin/python3 "$(command -v python3 2>/dev/null)"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            PYTHON="$candidate"
            return 0
        fi
    done
    return 1
}
