#!/usr/bin/env python3
"""Sign an AIOS catalogue manifest with an Ed25519 private key.

The appliance verifies it against the public key an administrator configured for
that repository (`public_key` in its provider options). The private key stays on
the machine that publishes the catalogue; none ships with AIOS.
"""
import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('manifest', type=Path, help='the AIOS manifest to sign (schema_version 1)')
parser.add_argument('--private-key', required=True, type=Path)
parser.add_argument('--output', type=Path, help='where to write the signed manifest (default: in place)')
args = parser.parse_args()

key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
if not isinstance(key, Ed25519PrivateKey):
    parser.error('An Ed25519 PEM private key is required')
document = json.loads(args.manifest.read_text())
payload = {k: v for k, v in document.items() if k != 'signature'}
document['signature'] = base64.b64encode(key.sign(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())).decode()
(args.output or args.manifest).write_text(json.dumps(document, indent=2) + '\n')
print('Signed', args.output or args.manifest)
