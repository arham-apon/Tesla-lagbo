#!/usr/bin/env sh
# Generates the RS256 key pair used to sign (Identity) and verify (Gateway, Notification) JWTs.
set -e
cd "$(dirname "$0")/.."
mkdir -p keys
openssl genrsa -out keys/jwt_private.pem 2048 && openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
echo "Wrote keys/jwt_private.pem and keys/jwt_public.pem"
