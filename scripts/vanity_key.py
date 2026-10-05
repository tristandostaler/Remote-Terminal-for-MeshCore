#!/usr/bin/env python3
"""Find a MeshCore identity key whose public key starts with a hex prefix.

This is a *vanity* generator: it repeatedly creates fresh random keypairs and
keeps the first one whose Ed25519 public key begins with the target prefix.
It does NOT recover a private key from an existing public key (that would be
breaking Ed25519 and is infeasible).

Key format notes (matches app/decoder.py:derive_public_key and how MeshCore
firmware / MeshCore Open Advanced derive an identity from a seed):
  - A 32-byte random *seed* is the root secret.
  - SHA-512(seed) -> 64 bytes; first 32 = scalar (clamped), last 32 = prefix.
  - public_key = scalar * basepoint  (noclamp; scalar is already clamped).
  - MeshCore stores the private key in *expanded* 64-byte form: scalar||prefix.

The script prints both the 32-byte seed and the 64-byte expanded private key
so whichever import path you use (radio seed import vs. this app's keystore)
has what it needs.

Usage:
    python scripts/vanity_key.py deadbeef
    python scripts/vanity_key.py deadbeef --workers 8
    python scripts/vanity_key.py cafe --case-insensitive

Cost: each extra hex char multiplies expected work by 16. 'deadbeef' is 32
bits -> ~2**32 (~4.3 billion) attempts on average.
"""

import argparse
import hashlib
import os
import sys
import time
from multiprocessing import Event, Process, Queue

import nacl.bindings


def expand_seed(seed: bytes) -> tuple[bytes, bytes]:
    """Return (public_key_32, expanded_private_key_64) for a 32-byte seed."""
    h = hashlib.sha512(seed).digest()
    scalar = bytearray(h[:32])
    # Standard Ed25519 clamp.
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    scalar = bytes(scalar)
    prefix = h[32:]
    public = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(scalar)
    return public, scalar + prefix


def _worker(prefix: str, case_insensitive: bool, found: Event, out: Queue, counter: Queue) -> None:
    norm = (str.lower if case_insensitive else (lambda s: s))
    target = norm(prefix)
    need = len(target)
    tried = 0
    while not found.is_set():
        seed = os.urandom(32)
        public = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(
            _clamped_scalar(seed)
        )
        tried += 1
        if norm(public.hex()[:need]) == target:
            _, expanded = expand_seed(seed)
            if not found.is_set():
                found.set()
                out.put((seed.hex(), public.hex(), expanded.hex()))
            break
        if tried % 50000 == 0:
            counter.put(50000)
    counter.put(tried % 50000)


def _clamped_scalar(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    scalar = bytearray(h[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return bytes(scalar)


def main() -> int:
    ap = argparse.ArgumentParser(description="MeshCore vanity public-key finder")
    ap.add_argument("prefix", help="target hex prefix, e.g. deadbeef")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument(
        "--case-insensitive",
        action="store_true",
        help="match prefix ignoring case (slightly faster to hit)",
    )
    args = ap.parse_args()

    prefix = args.prefix.lower() if args.case_insensitive else args.prefix
    valid = set("0123456789abcdefABCDEF")
    if not prefix or any(c not in valid for c in prefix):
        print(f"error: {args.prefix!r} is not valid hex", file=sys.stderr)
        return 2

    bits = len(prefix) * 4
    print(
        f"Searching for public key prefix {prefix!r} "
        f"({len(prefix)} hex chars = {bits} bits, "
        f"~2**{bits} ≈ {2 ** bits:,} attempts expected) "
        f"on {args.workers} worker(s)...",
        file=sys.stderr,
    )

    found = Event()
    out: Queue = Queue()
    counter: Queue = Queue()
    procs = [
        Process(
            target=_worker,
            args=(prefix, args.case_insensitive, found, out, counter),
            daemon=True,
        )
        for _ in range(args.workers)
    ]
    start = time.time()
    for p in procs:
        p.start()

    total = 0
    try:
        while not found.is_set():
            try:
                total += counter.get(timeout=1.0)
            except Exception:
                pass
            elapsed = time.time() - start
            if elapsed > 0:
                rate = total / elapsed
                print(
                    f"\r  {total:,} tried  |  {rate:,.0f}/s  |  {elapsed:,.0f}s elapsed",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
    except KeyboardInterrupt:
        found.set()
        print("\ninterrupted", file=sys.stderr)
        return 130

    seed_hex, public_hex, expanded_hex = out.get()
    elapsed = time.time() - start
    print(f"\n\nFound in {elapsed:,.1f}s after ~{total:,} attempts:\n", file=sys.stderr)
    print(f"public_key   : {public_hex}")
    print(f"seed (32B)   : {seed_hex}")
    print(f"private (64B): {expanded_hex}")
    print(
        "\n# private (64B) is the MeshCore expanded form (scalar||prefix) accepted by\n"
        "# this app's keystore / PUT /api/radio/private-key. seed (32B) is the root\n"
        "# secret for seed-style imports.",
        file=sys.stderr,
    )

    for p in procs:
        p.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
