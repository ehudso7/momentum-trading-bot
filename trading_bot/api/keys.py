import argparse
import hashlib
import json
import os
import secrets
from datetime import datetime

def _now():
    return datetime.utcnow().isoformat() + "Z"

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def _append_jsonl(path: str, record: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")

def issue(args):
    api_key = secrets.token_urlsafe(32)
    key_hash = _hash_key(api_key)

    record = {
        "created_at": _now(),
        "key_hash": key_hash,
        "label_hash": _hash_key(args.label),
        "tier": args.tier,
        "checkout_session_id": None,
        "ref_code": None,
    }

    _append_jsonl(args.manifest_path, record)

    print("API key issued (record this once; it cannot be recovered):")
    print(f"api_key      : {api_key}")
    print(f"key_hash     : {key_hash}")
    print(f"tier         : {args.tier}")
    print(f"created_at   : {record['created_at']}")
    print(f"label_hash   : {record['label_hash']}")
    print(f"manifest_path: {args.manifest_path}")

def list_keys(args):
    if not os.path.exists(args.manifest_path):
        print("No keys found.")
        return

    with open(args.manifest_path) as f:
        for line in f:
            print(line.strip())

def revoke(args):
    record = {
        "revoked_at": _now(),
        "key_hash": args.key_hash,
        "reason": args.reason,
    }
    _append_jsonl(args.revoked_path, record)
    print(f"Revoked key_hash={args.key_hash}")

def revoke_many(args):
    for key_hash in args.key_hash:
        record = {
            "revoked_at": _now(),
            "key_hash": key_hash,
            "reason": args.reason,
        }
        _append_jsonl(args.revoked_path, record)
        print(f"Revoked key_hash={key_hash}")

def build_parser():
    parser = argparse.ArgumentParser(prog="keys")
    sub = parser.add_subparsers(dest="command", required=True)

    # issue
    p = sub.add_parser("issue")
    p.add_argument("--tier", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--manifest-path", required=True)
    p.set_defaults(func=issue)

    # list
    p = sub.add_parser("list")
    p.add_argument("--manifest-path", required=True)
    p.set_defaults(func=list_keys)

    # revoke
    p = sub.add_parser("revoke")
    p.add_argument("--key-hash", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--revoked-path", required=True)
    p.set_defaults(func=revoke)

    # revoke-many
    p = sub.add_parser("revoke-many")
    p.add_argument("--key-hash", action="append", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--revoked-path", required=True)
    p.set_defaults(func=revoke_many)

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
