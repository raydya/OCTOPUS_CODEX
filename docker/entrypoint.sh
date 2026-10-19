#!/bin/sh
set -eu
umask 077

# Local Compose secrets may be root-owned bind mounts. Copy only the
# explicitly granted secrets into private tmpfs before dropping privileges.
install -d -o octopus -g octopus -m 0700 /run/octopus
for secret_name in postgres_runtime_password codex_token feishu_app_secret query_token; do
    secret_source="/run/secrets/$secret_name"
    if [ ! -f "$secret_source" ]; then
        printf 'Missing required secret file: %s\n' "$secret_name" >&2
        exit 1
    fi
    install -o octopus -g octopus -m 0400 "$secret_source" "/run/octopus/$secret_name"
done

if [ "$#" -eq 0 ]; then
    set -- /opt/venv/bin/python -m octopus run --wait-for-config
fi
exec gosu octopus "$@"
