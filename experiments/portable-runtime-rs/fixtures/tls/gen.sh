#!/usr/bin/env bash
# Regenerates the TEST-ONLY TLS fixtures. Not used at build or test time.
# Requires openssl. 36500 days (100 years) so the tests never expire in practice.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
tmp=$(mktemp -d)
DAYS=36500

ca() { # ca NAME CN
  openssl ecparam -name prime256v1 -genkey -noout -out "$tmp/$1.key"
  openssl req -new -key "$tmp/$1.key" -subj "/CN=$2" -out "$tmp/$1.csr"
  printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n' >"$tmp/$1.ext"
  openssl x509 -req -in "$tmp/$1.csr" -signkey "$tmp/$1.key" -days $DAYS -extfile "$tmp/$1.ext" -out "$1.pem"
}

ca ca "mctl-runtime test CA (not for production)"
ca other-ca "mctl-runtime unrelated test CA (not for production)"

# Leaf: CA:FALSE, SAN for 127.0.0.1 and localhost, serverAuth, PKCS#8 key.
openssl ecparam -name prime256v1 -genkey -noout -out "$tmp/server.sec1.key"
openssl pkcs8 -topk8 -nocrypt -in "$tmp/server.sec1.key" -out server.key.pem
openssl req -new -key "$tmp/server.sec1.key" -subj "/CN=localhost" -out "$tmp/server.csr"
cat >"$tmp/server.ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=IP:127.0.0.1,DNS:localhost
EXT
openssl x509 -req -in "$tmp/server.csr" -CA ca.pem -CAkey "$tmp/ca.key" -CAserial "$tmp/ca.srl" -CAcreateserial \
  -days $DAYS -extfile "$tmp/server.ext" -out server.pem
