#!/bin/sh
# Installed Cosign only: the Python verifier pins the image and release identity.
set -eu
gateway_script_dir=$(dirname -- "$0")
exec python3 "$gateway_script_dir/verify_litellm_image.py" "$@"
