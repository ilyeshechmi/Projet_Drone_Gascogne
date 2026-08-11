#!/bin/zsh

SCRIPT_DIR="${0:A:h}"
# Recherche l'environnement à côté de l'application ou de ses dossiers parents.
if [[ -x "$SCRIPT_DIR/../env_drone/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/../env_drone/bin/python"
elif [[ -x "$SCRIPT_DIR/env_drone/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/env_drone/bin/python"
elif [[ -x "$SCRIPT_DIR/../../env_drone/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/../../env_drone/bin/python"
else
    PYTHON="$(command -v python3)"
fi

cd "$SCRIPT_DIR" || exit 1
exec "$PYTHON" "$SCRIPT_DIR/drone_photo_importer.py" "$@"
