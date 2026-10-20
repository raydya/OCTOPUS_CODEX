#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SECRETS_DIR="$PROJECT_ROOT/.secrets"

if [[ -L "$SECRETS_DIR" ]]; then
    printf 'The .secrets directory must not be a symbolic link.\n' >&2
    exit 1
fi
mkdir -p -- "$SECRETS_DIR"
chmod 0700 "$SECRETS_DIR"

if [[ ! -e "$PROJECT_ROOT/.env" ]]; then
    if [[ ! -f "$PROJECT_ROOT/.env.example" ]]; then
        printf 'Missing .env.example.\n' >&2
        exit 1
    fi
    (set -o noclobber; cat -- "$PROJECT_ROOT/.env.example" > "$PROJECT_ROOT/.env")
fi
if [[ -L "$PROJECT_ROOT/.env" || ! -f "$PROJECT_ROOT/.env" ]]; then
    printf 'The .env path must be a regular file.\n' >&2
    exit 1
fi
chmod 0600 "$PROJECT_ROOT/.env"

for secret_name in postgres_owner_password postgres_runtime_password codex_token feishu_app_secret query_token; do
    secret_path="$SECRETS_DIR/$secret_name"
    if [[ -L "$secret_path" ]]; then
        printf 'Secret path must not be a symbolic link: %s\n' "$secret_name" >&2
        exit 1
    fi
    if [[ ! -e "$secret_path" ]]; then
        if [[ "$secret_name" == feishu_app_secret ]]; then
            (set -o noclobber; : > "$secret_path")
        else
            (set -o noclobber; openssl rand -hex 32 > "$secret_path")
        fi
    fi
    if [[ ! -f "$secret_path" ]]; then
        printf 'Secret path must be a regular file: %s\n' "$secret_name" >&2
        exit 1
    fi
    chmod 0600 "$secret_path"
done

printf 'Local configuration is ready. Existing values were preserved.\n'
printf 'Fill .env and .secrets/feishu_app_secret for the new Feishu application.\n'
printf 'Have local-infra provision octopus_owner and octopus_runtime using the matching secret files before maintenance.\n'
