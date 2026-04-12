"""Credify pipeline — single-process orchestrator. No subprocess calls."""
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from parsers import detect_provider, validate_or_die
from classify import partition_accounts, resolve_client_name, resolve_client_last, safe_filename
from strategize import build_context, needs_tradelines
from renderer import render
from tradelines import fetch_tradelines, find_best_combo


def _validate_output_path(path, label):
    """Reject output paths outside user home directory."""
    resolved = os.path.realpath(os.path.expanduser(path))
    home = os.path.expanduser("~")
    if not resolved.startswith(home):
        raise ValueError(f"{label} path must be under user home directory: {resolved}")
    return resolved


def run(file_path, *, disputes=False, mail=False, live=False,
        out_strategy=None, out_json=None, out_disputes=None, score_drop=None,
        discount=None, discount_note=None):
    """Single-process pipeline. Returns dict of output paths and timing."""
    times = {}
    t0 = time.time()

    # Validate output paths
    if out_strategy: out_strategy = _validate_output_path(out_strategy, "--out")
    if out_json: out_json = _validate_output_path(out_json, "--json")
    if out_disputes: out_disputes = _validate_output_path(out_disputes, "--disputes-dir")

    # Stage 1: Parse + tradeline pre-fetch (parallel)
    t = time.time()
    tl_executor = ThreadPoolExecutor(1)
    tl_future = tl_executor.submit(fetch_tradelines, min_limit=10000)

    provider = detect_provider(file_path)
    print(f"Provider: {provider.PROVIDER_NAME}", file=sys.stderr)
    data = provider.parse(file_path)
    validate_or_die(data)
    times["parse"] = time.time() - t
    print(f"Parsed: {len(data.get('raw_accounts', []))} accounts in {times['parse']*1000:.0f}ms", file=sys.stderr)

    # Stage 2: Classify — single pass
    t = time.time()
    derog, clean = partition_accounts(data.get("raw_accounts", []))
    times["classify"] = time.time() - t

    # Stage 3: Tradelines (await pre-fetch)
    t = time.time()
    need_tl, aaoa_mo, big_rev = needs_tradelines(clean)
    combo = None
    if need_tl:
        tradelines = tl_future.result()
        print(f"  {len(tradelines)} tradelines loaded", file=sys.stderr)
        if len(tradelines) >= 2:
            combo = find_best_combo(tradelines, data["raw_accounts"])
            if combo:
                print(f"  Best combo: {combo['pick1']['bank']} + {combo['pick2']['bank']} = ${combo['combo_price']:.0f}", file=sys.stderr)
    tl_executor.shutdown(wait=False)
    times["tradelines"] = time.time() - t

    # Stage 4: Build context + render
    t = time.time()
    ctx = build_context(data, derog, clean, combo, score_drop=score_drop,
                        discount=discount, discount_note=discount_note)
    times["context"] = time.time() - t

    t = time.time()
    client = data.get("client", {})
    last_name = safe_filename(resolve_client_last(client))
    strategy_path = out_strategy or os.path.expanduser(f"~/Desktop/{last_name} Credit Repair Strategy.html")
    strategy_path = render(ctx, strategy_path)
    times["render"] = time.time() - t
    print(f"Strategy: {strategy_path}", file=sys.stderr)

    # Stage 5: JSON dump (optional, with 0600 perms)
    if out_json:
        import json
        fd = os.open(out_json, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"JSON: {out_json}", file=sys.stderr)

    # Stage 6: Disputes (optional — fail loudly if requested)
    dispute_paths = []
    if disputes:
        t = time.time()
        from disputes import generate_disputes
        dispute_dir = out_disputes or os.path.expanduser(f"~/Desktop/disputes_{last_name}")
        dispute_paths = generate_disputes(data, derog, dispute_dir)
        times["disputes"] = time.time() - t
        print(f"Disputes: {len(dispute_paths)} letters in {dispute_dir}", file=sys.stderr)

    # Stage 7: Mail (optional — fail loudly on auth/SSL, warn on transient)
    if mail and dispute_paths:
        t = time.time()
        from mailer import batch_send
        batch_send(dispute_paths, client, live=live)
        times["mail"] = time.time() - t
        print(f"Mailed: {len(dispute_paths)} letters", file=sys.stderr)

    total = time.time() - t0
    print(f"Total: {total:.1f}s ({' + '.join(f'{k}={v*1000:.0f}ms' for k, v in times.items())})", file=sys.stderr)

    return {"strategy": strategy_path, "disputes": dispute_paths, "times": times}
