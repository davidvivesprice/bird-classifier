#!/bin/bash
# Wrapper that loads API keys from ~/.bird-observatory-env before running
# a Python script. Used by LaunchAgent plists so keys stay out of plist
# files (which show up in `ps` output and are world-readable by default).

ENV_FILE="$HOME/.bird-observatory-env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found" >&2
    exit 1
fi

# Export every KEY=VALUE line (skip blanks and comments)
while IFS='=' read -r key value; do
    # Skip empty lines and comments
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    # Map generic name to the env var the scripts expect
    case "$key" in
        UNIFI_API_KEY)
            export UNIFI_PROTECT_API_KEY="$value"
            ;;
        *)
            export "$key=$value"
            ;;
    esac
done < "$ENV_FILE"

exec "$@"
