#!/usr/bin/env python3
"""Load the BEAM corpus through the SDK on scroll-2, one store per conversation.

Each turn is one `add` with who said it (`speaker`) and when it happened
(`event_date`). A date cannot be added afterwards without loading everything
again.

`event_date` is the one to get right. `time_anchor` appears only on the turn
that opens a session and applies to the rest of it, so the value is carried
forward here. Drop it and every memory lands with the ingest date: the corpus
still loads, search still returns sensible text, and every temporal and ordering
question becomes unanswerable without anything reporting an error.

On scroll-2 a store takes a memory only once it has a time zone, and only from a
speaker it knows. So each store is set up before its first turn:

    create_store(store)
    add_speaker("user", store)          the person in the conversation
    add_speaker("assistant", store)     the assistant, who "me" stands for
    set_settings(store, time_zone="UTC", default_speakers={"user": ..., "assistant": ...})

and then every turn is

    add(content, store, metadata={"speaker": "user" or "me", "event_date": ..., "category": ...})

"me" marks the assistant's own words, as in the published tablet-2 ingest. The
benchmark gives dates without a time zone; UTC keeps each date on its own day.

Turns are sent whole. Each write carries an idempotency key made from the store
and the turn's position, so running this again within a day after it stopped
partway replays the turns already stored instead of storing them twice.

Usage:
    export WOS_API_KEY=...
    export BEAM_PARQUET=/path/to/1M.parquet
    export CONV=35                      # conversations to load
    python3 beam_ingest.py
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from wontopos import Client
except ImportError:
    raise SystemExit("The wontopos SDK is missing. Install it with: pip install wontopos")

PARQUET = os.environ.get("BEAM_PARQUET", "1M.parquet")   # the benchmark's own corpus file
BASE = os.environ.get("WOS_BASE_URL")                    # unset: the SDK's default endpoint
STORE = os.environ.get("BEAM_STORE_FORMAT", "beam1m_{n}")  # {n}: the conversation number
CONV = int(os.environ.get("CONV", "5"))                  # conversations to load
CONC = int(os.environ.get("CONC", "8"))                  # stores written in parallel
MODEL = "scroll-2"
TIME_ZONE = "UTC"
USER = "user"
ASSISTANT = "assistant"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def rfc3339(anchor):
    """'March-01-2024' -> '2024-03-01T00:00:00Z'. Unparseable input (the corpus
    writes 'None' on turns without an anchor) returns None, and the caller keeps
    the previous date rather than inventing one."""
    try:
        mon, day, year = str(anchor).split("-")
        return f"{int(year):04d}-{MONTHS[mon]:02d}-{int(day):02d}T00:00:00Z"
    except Exception:
        return None


def turns_of(row):
    """Every non-empty turn of one conversation, in order, as (content, metadata)."""
    category = str(dict(row["conversation_seed"]).get("category") or "general").lower()
    out, date = [], None
    for session in row["chat"]:
        for turn in session:
            d = dict(turn)
            if d.get("time_anchor"):
                date = rfc3339(d["time_anchor"]) or date
            content = str(d.get("content") or "").strip()
            if not content:
                continue
            md = {"category": category,
                  "speaker": "me" if d.get("role") == "assistant" else USER}
            if date:
                md["event_date"] = date
            out.append((content, md))
    return out


def prepare(client, store):
    """A store with its time zone and its two speakers, ready to take turns."""
    client.create_store(store)
    client.add_speaker(USER, store)
    client.add_speaker(ASSISTANT, store)
    client.set_settings(store, time_zone=TIME_ZONE,
                        default_speakers={"user": USER, "assistant": ASSISTANT})


def load(client, store, turns, report):
    """Write one store's turns in order. Returns the failures as (position, error)."""
    failed = []
    for i, (content, md) in enumerate(turns):
        try:
            client.add(content, store, metadata=md, idempotency_key=f"{store}:{i}")
            report(True)
        except Exception as e:
            failed.append((i, str(e)[:200]))
            report(False)
    return failed


def main():
    import pandas as pd

    client = Client(api_key=os.environ["WOS_API_KEY"], model=MODEL, timeout=300,
                    **({"base_url": BASE} if BASE else {}))

    df = pd.read_parquet(PARQUET)
    by_store = {}
    for _, row in df.head(CONV).iterrows():
        by_store[STORE.format(n=row["conversation_id"])] = turns_of(row)
    total = sum(len(t) for t in by_store.values())
    print(f"{CONV} conversations - {len(by_store)} stores - {total:,} turns "
          f"- {CONC} stores in parallel", flush=True)

    # Set up every store before writing any turn: a store that cannot be set up
    # would refuse every turn sent to it.
    for store in by_store:
        try:
            prepare(client, store)
        except Exception as e:
            sys.exit(f"store {store} could not be set up: {e}")

    done = [0, 0]
    lock = threading.Lock()
    t0 = time.time()

    def report(ok):
        with lock:
            done[0 if ok else 1] += 1
            n = done[0] + done[1]
        if n % 200 == 0:
            el = time.time() - t0
            rate = done[0] / max(el, .01)
            print(f"  {n:,}/{total:,} - {rate:.1f}/s - "
                  f"{(total - n) / max(rate, .01) / 60:.0f} min left", flush=True)

    # Sequential within a store, parallel across stores. A store takes one write
    # at a time, and since a conversation is a store, parallelising turns would
    # send every worker at the same store and most would be refused.
    with ThreadPoolExecutor(max_workers=min(CONC, len(by_store))) as ex:
        failures = dict(zip(by_store, ex.map(lambda s: load(client, s, by_store[s], report),
                                             by_store)))

    el = time.time() - t0
    print(f"\nstored {done[0]:,}, failed {done[1]:,} - {el / 60:.1f} min "
          f"- {done[0] / max(el, .01):.1f}/s")
    print(f"stores: {', '.join(by_store)}")
    for store, failed in failures.items():
        for i, err in failed[:3]:
            print(f"  failed: {store} turn {i}: {err}")
    print("\nRun verify_corpus.py before scoring on this. An ingest that exits "
          "cleanly has reported what it sent, not what can be read back.")
    if done[1]:
        sys.exit(1)


if __name__ == "__main__":
    main()
