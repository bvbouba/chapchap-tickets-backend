#!/bin/sh
set -eu

# Load multiline PEM keys from files (env_file cannot hold PEMs reliably).
if [ -f /run/secrets/rsa_private.pem ]; then
  RSA_PRIVATE_KEY="$(cat /run/secrets/rsa_private.pem)"
  export RSA_PRIVATE_KEY
fi
if [ -f /run/secrets/rsa_public.pem ]; then
  RSA_PUBLIC_KEY="$(cat /run/secrets/rsa_public.pem)"
  export RSA_PUBLIC_KEY
fi

exec "$@"
