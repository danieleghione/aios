#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

parser=argparse.ArgumentParser(description='Sign an AIOS component release with an external Ed25519 private key')
parser.add_argument('component',choices=['application','runtime','open-webui','platform'])
parser.add_argument('version')
parser.add_argument('archive',type=Path)
parser.add_argument('--private-key',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
args=parser.parse_args()
key=serialization.load_pem_private_key(args.private_key.read_bytes(),password=None)
if not isinstance(key,Ed25519PrivateKey):
    parser.error('An Ed25519 PEM private key is required')
with args.archive.open('rb') as stream:
    sha=hashlib.file_digest(stream,'sha256').hexdigest()
manifest={'component':args.component,'version':args.version,'sha256':sha}
canonical=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
args.output.write_text(json.dumps({'manifest':manifest,'signature':base64.b64encode(key.sign(canonical)).decode()},indent=2)+'\n')
