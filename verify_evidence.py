"""Check ScopePay evidence offline, with nothing but the public key. For merchants, PayPal or an arbiter.

  python verify_evidence.py EV-1A2B3C4D.json --key <public_key_b64>
  python verify_evidence.py trail.json --key-file public_key.json

Input: an evidence bundle downloaded from an evidence page (JSON), or the full trail from GET /api/evidence.
Key:   GET /api/evidence/public-key (the public_key_b64 field, or save the whole response as JSON).
For a single bundle each record's hash and signature are checked; for the full trail the chain links too.
Exit code 0 when everything verifies, 1 otherwise. Needs only the `cryptography` package.
"""
import argparse
import hashlib
import json
import sys

import signing

SIG_FIELDS = ("hash", "sig", "sig_alg", "key_id")


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_record(r, public_key_b64):
    body = {k: v for k, v in r.items() if k not in SIG_FIELDS}
    if hashlib.sha256(_canon(body).encode()).hexdigest() != r.get("hash"):
        return "hash does not match the contents (record edited)"
    if not r.get("sig"):
        return "not signed"
    if not signing.verify(r["hash"], r["sig"], public_key_b64):
        return "signature invalid (not written by the holder of this key, or edited)"
    return None


def records_of(doc):
    if isinstance(doc, list):
        return doc, True
    if "records" in doc:  # GET /api/evidence returns newest first
        return sorted(doc["records"], key=lambda r: r["seq"]), True
    recs = [doc.get("record"), doc.get("code_issuance"), *doc.get("related", []), *doc.get("escrow_actions", [])]
    return [r for r in recs if r], False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("evidence")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="public_key_b64 from /api/evidence/public-key")
    g.add_argument("--key-file", help="saved response of /api/evidence/public-key")
    a = ap.parse_args(argv)
    if not signing.AVAILABLE:
        sys.exit("Install the cryptography package first:  pip install cryptography")
    key = a.key or json.load(open(a.key_file))["public_key_b64"]
    records, full = records_of(json.load(open(a.evidence)))
    bad = 0
    prev = "GENESIS"
    for r in records:
        problem = check_record(r, key)
        if not problem and full and r.get("prev_hash") != prev:
            problem = "chain link broken (a record before this one was removed, added or reordered)"
        prev = r.get("hash")
        bad += bool(problem)
        print(f"{'✗' if problem else '✓'} #{r.get('seq')} {r.get('id')} {r.get('type')}" + (f"  — {problem}" if problem else ""))
    scope = "full trail, chain links checked" if full else "bundle: each record checked on its own"
    print(f"\n{len(records) - bad}/{len(records)} records verified ({scope}).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
