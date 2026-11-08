#!/bin/sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
action=${1:-start}
if [ "$#" -gt 0 ]; then shift; fi
env_file=${AION_IMAGE_MCP_ENV_FILE:-"$HOME/.aion-openai-image-mcp/.env"}

case "$action" in
    start|restart)
        if [ -f "$env_file" ]; then
            exec uv run --project "$project_dir" --locked --env-file "$env_file" aion-image-mcp-service "$action" "$@"
        fi
        ;;
    stop|status) ;;
    *)
        printf 'Usage: %s [start|stop|restart|status] [--config PATH]\n' "$0" >&2
        exit 2
        ;;
esac

exec uv run --project "$project_dir" --locked aion-image-mcp-service "$action" "$@"
