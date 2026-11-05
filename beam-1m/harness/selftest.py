#!/usr/bin/env python3
"""A check of the harness that calls neither the memory service nor any model.

The wontopos SDK found on the Python path talks to a fake memory service on the loopback interface, and fake
readers and a fake judge answer. Nothing of the SDK is faked. What is checked:

  build_tasks.py    probing questions become tasks of the shape run.py and grade.py read; a question with no
                    ground truth is reported, not dropped
  beam_ingest.py    each store gets its time zone and speakers before its first turn (the fake service refuses a
                    write otherwise, as scroll-2 does); every turn arrives in order with its speaker, the date
                    carried forward from its session's anchor, and an idempotency key of its own
  verify_corpus.py  counts, dates and speakers read back; a store without dates is reported
  run.py            with each reader (claude-cli, anthropic, openai): the prompt byte for byte, the tools offered
                    against the SDK's definitions, every tool result against the SDK's text, the preset; every
                    request the SDK sent is one the harness's public calls make, on scroll-2, with no preview
                    header; no reference answer, rubric or key in the run's files before grading; the claude-cli
                    flags, environment, working folder and turn check; the manifest
  grade.py          the grading arithmetic, with both ways of cutting event-ordering lines
  analyze.py        one column per tool of the run
  the sources       no private attribute of the SDK, no preview header, no patching

With pandas installed, build_tasks.py, beam_ingest.py and verify_corpus.py also run whole on a small parquet file
made here.

With --record DIR, every per-question record in DIR (a run of this harness or its predecessor) is rebuilt: the
prompt from its first list and question, with the SDK's note, must have the record's token count (tiktoken
needed), the note must have the hash of beam700_sdkoff1019, and the tool definitions that of SDK 2.2.49.

usage: selftest.py [--record DIR] [--keep]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_tasks  # noqa: E402
import run as harness  # noqa: E402

SOURCES = ("build_tasks.py", "beam_ingest.py", "verify_corpus.py", "run.py", "mcp_proxy.py", "grade.py",
           "analyze.py")
KEY = "wos-selftest-key"
READER_KEY = "selftest-reader-key"
GT_MARK = "SELFTEST-REFERENCE-ANSWER"
RUBRIC_MARK = "SELFTEST-RUBRIC"
# The paths the harness's public SDK calls reach: create_store, add_speaker, set_settings, add, stats, search, and
# the memory tools' begin, run and answered.
SDK_PATHS = {"/api/v1/memory/" + p for p in ("collection", "speakers", "settings", "store", "stats", "search",
                                               "work/open", "work/deeper", "work/search", "work/around",
                                               "work/close")}

# What the fake service finds for each question, and what the fake readers ask for, turn by turn. The claude-cli
# reader makes the same calls one per turn.
PLANS = {
    "What colour was the bike?": {
        "open": ["I bought a bike last spring."],
        "turns": [[{"name": "memory_search", "input": {"query": "the bike's colour"}, "found": ["The bike is red."]}],
                  [{"name": "memory_search", "input": {"query": "when the bike was bought"},
                    "refuse": "no steps left"}]],
        "answer": "Red [02]."},
    "In what order did the three trips happen?": {
        "open": ["Kyoto was the last trip."],
        "turns": [[{"name": "memory_deeper", "input": {}, "found": ["Lisbon came first, then Oslo."]},
                   {"name": "memory_around", "input": {"n": 1}, "pieces": ["We planned Oslo before Kyoto."]}]],
        "answer": "Trip to Kyoto\n\nTrip to Oslo\nTrip to Lisbon"},
}


def calls_of(plan):
    return [c for turn in plan["turns"] for c in turn]


def plan_for(prompt):
    return next(p for q, p in PLANS.items() if "QUESTION\n" + q + "\n" in prompt)


def turn(role, content, anchor):
    return {"role": role, "content": content, "time_anchor": anchor}


# Three conversations as the benchmark's parquet holds them. The third has no dates at all, and one probing
# question with no ground truth under any known field name.
CORPUS = [
    {"conversation_id": "1", "conversation_seed": {"category": "Coding"},
     "chat": [[turn("user", "I bought a bike.", "March-01-2024"), turn("assistant", "Nice.", "None"),
               turn("user", "   ", "None")],
              [turn("user", "The bike is red.", "April-15-2024"), turn("assistant", "Noted.", "None")]],
     "probing_questions": repr({"information_extraction": [
         {"question": "What colour was the bike?", "answer": GT_MARK + " red", "difficulty": "easy",
          "rubric": [RUBRIC_MARK + " names the red bike", RUBRIC_MARK + " HALF says it was bought in 2021"]}]})},
    {"conversation_id": "2", "conversation_seed": {"category": "Travel"},
     "chat": [[turn("user", "Kyoto was last.", "None")],
              [turn("user", "Lisbon came first.", "May-02-2024"), turn("assistant", "Then Oslo.", "None")]],
     "probing_questions": repr({"event_ordering": [
         {"question": "In what order did the three trips happen?", "ideal_response": GT_MARK + " order",
          "difficulty": "hard", "rubric": ["Trip to Lisbon", "Trip to Oslo", "Trip to Kyoto"]}]})},
    {"conversation_id": "3", "conversation_seed": {"category": "General"},
     "chat": [[turn("user", "No date here.", "None"), turn("assistant", "None either.", "None")]],
     "probing_questions": repr({"summarization": [{"question": "Sum it up.", "rubric": ["anything"]}]})},
]
MARCH, APRIL, MAY = "2024-03-01T00:00:00Z", "2024-04-15T00:00:00Z", "2024-05-02T00:00:00Z"
TURNS = {
    "1": [("I bought a bike.", {"category": "coding", "speaker": "user", "event_date": MARCH}),
          ("Nice.", {"category": "coding", "speaker": "me", "event_date": MARCH}),
          ("The bike is red.", {"category": "coding", "speaker": "user", "event_date": APRIL}),
          ("Noted.", {"category": "coding", "speaker": "me", "event_date": APRIL})],
    "2": [("Kyoto was last.", {"category": "travel", "speaker": "user"}),
          ("Lisbon came first.", {"category": "travel", "speaker": "user", "event_date": MAY}),
          ("Then Oslo.", {"category": "travel", "speaker": "me", "event_date": MAY})],
    "3": [("No date here.", {"category": "general", "speaker": "user"}),
          ("None either.", {"category": "general", "speaker": "me"})],
}


class Service:
    """The memory service as far as the harness reaches it. Writes are refused as scroll-2 refuses them: on a store
    with no time zone, or from a speaker the store does not know ("me" stands for its default assistant)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = []
        self.stores = {}
        self.works = {}

    def handle(self, path, headers, body):
        with self.lock:
            self.log.append({"path": path, "headers": headers, "body": body})
            uid = body.get("user_id")
            s = self.stores.get(uid)
            if path.endswith("/collection"):
                self.stores.setdefault(uid, {"tz": None, "speakers": set(), "defaults": {}, "memories": []})
                return 200, {"user_id": uid, "status": "created" if s is None else "exists"}
            if path.startswith("/api/v1/memory/work/"):
                return self.work(path.rsplit("/", 1)[-1], body)
            if s is None:
                return 404, {"error": f"Store '{uid}' does not exist."}
            if path.endswith("/speakers"):
                s["speakers"].add(body["speaker"])
                return 201, {"user_id": uid, "speaker": body["speaker"], "status": "registered"}
            if path.endswith("/settings"):
                defaults = body.get("default_speakers") or {}
                if any(v not in s["speakers"] for v in defaults.values()):
                    return 400, {"error": {"message": "unknown_speaker", "code": "unknown_speaker"}}
                s["tz"] = body.get("time_zone") or s["tz"]
                s["defaults"].update(defaults)
                return 200, {"status": "ok", "time_zone": s["tz"], "default_speakers": s["defaults"]}
            if path.endswith("/store"):
                md = body.get("metadata") or {}
                speaker = md.get("speaker")
                if not s["tz"]:
                    return 400, {"error": {"message": "time_zone_required", "code": "time_zone_required"}}
                if not (speaker in s["speakers"] or (speaker == "me" and s["defaults"].get("assistant"))):
                    return 400, {"error": {"message": "unknown_speaker", "code": "unknown_speaker"}}
                mid = f"{uid}/{len(s['memories'])}"
                s["memories"].append(dict(md, id=mid, content=body["content"]))
                return 200, {"id": mid, "status": "stored"}
            if path.endswith("/stats"):
                return 200, {"user_id": uid, "total_memories": len(s["memories"])}
            if path.endswith("/search"):
                return 200, {"memories": s["memories"][:body.get("limit") or 10]}
            return 404, {"error": "selftest: no such path"}

    def numbered(self, work, contents):
        rows = []
        for c in contents:
            work["n"] += 1
            rows.append({"n": work["n"], "id": f"{work['id']}-{work['n']}", "content": c})
        return {"memories": rows, "steps_left": 5, "deeper_left": 3}

    def work(self, op, body):
        if op == "open":
            plan = PLANS[body["question"]]
            work = {"id": f"w{len(self.works)}", "calls": calls_of(plan), "i": 0, "n": 0}
            self.works[work["id"]] = work
            return 200, dict(self.numbered(work, plan["open"]), work_id=work["id"])
        work = self.works[body["work_id"]]
        if op == "close":
            return 200, {"closed": True, "work_id": work["id"]}
        want = work["calls"][work["i"]]
        work["i"] += 1
        if (want["name"] != "memory_" + op or body.get("query", "") != want["input"].get("query", "")
                or body.get("n") != want["input"].get("n")):
            return 400, {"error": f"selftest: unexpected step {op} {body}"}
        if "refuse" in want:
            return 400, {"error": want["refuse"]}
        if op == "around":
            return 200, {"pieces": [{"at": i + 1, "content": c} for i, c in enumerate(want["pieces"])],
                         "steps_left": 4, "deeper_left": 3}
        return 200, self.numbered(work, want["found"])


class Models:
    """The fake readers (Messages API, OpenAI-style chat completions) and the fake judge."""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = []

    def handle(self, path, headers, body):
        with self.lock:
            self.log.append({"path": path, "headers": headers, "body": body})
        if path == "/v1/messages":
            return 200, self.anthropic(body)
        if path == "/reader/v1/chat/completions":
            return 200, self.openai(body)
        return 200, self.judge(body)

    def anthropic(self, body):
        msgs = body["messages"]
        plan = plan_for(msgs[0]["content"])
        done = (len(msgs) - 1) // 2
        usage = {"input_tokens": 10, "output_tokens": 5}
        if done < len(plan["turns"]):
            uses = [{"type": "tool_use", "id": f"toolu_{done}_{i}", "name": c["name"], "input": c["input"]}
                    for i, c in enumerate(plan["turns"][done])]
            return {"model": body["model"], "stop_reason": "tool_use", "usage": usage,
                    "content": [{"type": "thinking", "thinking": "", "signature": "selftest"}] + uses}
        return {"model": body["model"], "stop_reason": "end_turn", "usage": usage,
                "content": [{"type": "text", "text": plan["answer"]}]}

    def openai(self, body):
        msgs = body["messages"]
        plan = plan_for(msgs[0]["content"])
        done = sum(1 for m in msgs if m["role"] == "assistant")
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if done < len(plan["turns"]):
            calls = [{"id": f"call_{done}_{i}", "type": "function",
                      "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                     for i, c in enumerate(plan["turns"][done])]
            message, finish = {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": plan["answer"]}, "stop"
        return {"model": body["model"], "usage": usage, "choices": [{"message": message, "finish_reason": finish}]}

    def judge(self, body):
        msgs = body["messages"]
        if msgs[0]["role"] == "system":
            ref, line = msgs[1]["content"][len("First snippet: "):].split(" \n\n Second snippet: ", 1)
            content = "YES" if ref.strip().lower() == line.strip().lower() else "NO"
        else:
            content = '{"score": %s, "reason": "selftest"}' % ("0.5" if " HALF" in msgs[0]["content"] else "1.0")
        return {"model": "selftest-judge", "choices": [{"message": {"content": content}}]}


def serve(target):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            code, obj = target.handle(self.path, headers, json.loads(raw or b"{}"))
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


FAKE_CLAUDE = r'''
import json, os, subprocess, sys
argv = sys.argv[1:]
if argv == ["--version"]:
    print("0.0.0 (selftest reader)")
    sys.exit(0)


def val(flag):
    return argv[argv.index(flag) + 1]


cfg = json.load(open(val("--mcp-config")))["mcpServers"]["m"]
qid = cfg["env"]["QID"]
seen = {"argv": argv, "cwd": os.path.realpath(os.getcwd()), "cwd_files": sorted(os.listdir(".")),
        "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_ORG_MEMORY",
                                               "WOS_API_KEY", "READER_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")},
        "prompt": sys.stdin.read()}
srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                       env={**os.environ, **cfg["env"]})
ids = [0]


def rpc(method, params=None):
    ids[0] += 1
    srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ids[0], "method": method, "params": params or {}}) + "\n")
    srv.stdin.flush()
    return json.loads(srv.stdout.readline())


rpc("initialize", {"protocolVersion": "2025-06-18"})
srv.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
srv.stdin.flush()
seen["tools"] = rpc("tools/list")["result"]["tools"]
names = ["mcp__m__" + t["name"] for t in seen["tools"]]
log = os.path.join(os.environ["SELFTEST_READER_LOG"], qid + ".json")
if val("--output-format") == "stream-json":
    json.dump(seen, open(log, "w"))
    print(json.dumps({"type": "system", "subtype": "init", "tools": names, "model": "selftest-reader",
                      "mcp_servers": [{"name": "m", "status": "connected"}], "claude_code_version": "0.0.0",
                      "apiKeySource": "none", "permissionMode": "default"}), flush=True)
    srv.terminate()
    sys.exit(0)
plan = json.load(open(os.environ["SELFTEST_PLAN"]))[qid]
seen["results"] = [rpc("tools/call", {"name": c["name"], "arguments": c["input"]})["result"] for c in plan["calls"]]
srv.stdin.close()
srv.wait()
json.dump(seen, open(log, "w"))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": plan["answer"],
                  "num_turns": len(plan["calls"]) + 1, "duration_ms": 1, "total_cost_usd": 0.0,
                  "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                            "output_tokens": 1},
                  "modelUsage": {"selftest-reader": {}}}))
'''


class Checks:
    def __init__(self):
        self.failed = []

    def __call__(self, ok, what):
        print(("PASS " if ok else "FAIL ") + what, flush=True)
        if not ok:
            self.failed.append(what)
        return ok


def check_sources(check):
    for f in SOURCES:
        text = open(os.path.join(HERE, f)).read()
        hits = re.findall(r"\._[A-Za-z]\w*", text) + re.findall(r"(?i)preview", text) \
            + re.findall(r"\bwontopos\.\w+(?:\.\w+)*\s*=(?!=)|setattr\(", text)
        check(not hits, f"{f}: no private SDK attribute, no preview header, no patching {hits or ''}")


def check_tasks(check):
    tasks, missing = [], []
    for row in CORPUS:
        got, lost = build_tasks.tasks_of(row["conversation_id"], row["probing_questions"])
        tasks += got
        missing += lost
    check([m[0] for m in missing] == ["3_summarization_0"], f"build_tasks: no ground truth is reported {missing}")
    check([t["question_id"] for t in tasks] == ["1_information_extraction_0", "2_event_ordering_0"]
          and all(set(t) == {"question_id", "user_id", "question", "gt", "rubric", "type", "difficulty"}
                  for t in tasks)
          and [t["user_id"] for t in tasks] == ["beam1m-1", "beam1m-2"]
          and tasks[0]["gt"] == GT_MARK + " red" and tasks[1]["rubric"] == ["Trip to Lisbon", "Trip to Oslo",
                                                                           "Trip to Kyoto"],
          "build_tasks: ids, stores, ground truth under either field name, rubric")
    return tasks


def check_ingest(check, wontopos, service, url):
    import beam_ingest
    import verify_corpus
    client = wontopos.Client(api_key=KEY, base_url=url, model=beam_ingest.MODEL)
    check(beam_ingest.MODEL == verify_corpus.MODEL == "scroll-2", "ingest and verify write and read on scroll-2")
    for row in CORPUS:
        cid = row["conversation_id"]
        turns = beam_ingest.turns_of(row)
        check(turns == TURNS[cid], f"beam_ingest: conversation {cid}, speakers and dates carried forward")
        store = f"beam1m_{cid}"
        start = len(service.log)
        beam_ingest.prepare(client, store)
        failed = beam_ingest.load(client, store, turns, lambda ok: None)
        check(failed == [], f"beam_ingest: {store} stored every turn {failed or ''}")
        sent = service.log[start:]
        check([r["path"].rsplit("/", 1)[-1] for r in sent] == ["collection", "speakers", "speakers", "settings"]
              + ["store"] * len(turns)
              and [r["body"].get("speaker") for r in sent[1:3]] == ["user", "assistant"]
              and sent[3]["body"].get("time_zone") == "UTC"
              and sent[3]["body"].get("default_speakers") == {"user": "user", "assistant": "assistant"},
              f"beam_ingest: {store} set up (time zone, two speakers) before its first turn")
        writes = sent[4:]
        check([(r["body"]["content"], r["body"]["metadata"]) for r in writes] == turns
              and [r["headers"].get("idempotency-key") for r in writes] == [f"{store}:{i}" for i in range(len(turns))]
              and all(r["headers"].get("x-wos-model") == "scroll-2" for r in sent),
              f"beam_ingest: {store} turns in order, each with its metadata and idempotency key, on scroll-2")
    client.create_store("beam1m_9")
    failed = beam_ingest.load(client, "beam1m_9", TURNS["1"], lambda ok: None)
    check(len(failed) == 4 and "time_zone_required" in failed[0][1],
          "the fake service refuses turns on a store that was not set up")

    rows = [verify_corpus.check(client, f"beam1m_{n}") for n in (1, 2, 3)]
    check([(r["count"], r["returned"], r["dated"], r["spoken"], r["earliest"]) for r in rows]
          == [(4, 4, "4/4", "4/4", "2024-03-01"), (3, 3, "2/3", "3/3", "2024-05-02"), (2, 2, "0/2", "2/2", None)],
          "verify_corpus: counts, dates and speakers read back")
    check(verify_corpus.problems(rows) == ["beam1m_3: no event dates, time questions cannot be answered"],
          "verify_corpus: the store without dates is reported")


def check_whole_scripts(check, tmp, env, service):
    """build_tasks.py, beam_ingest.py and verify_corpus.py run whole, on a parquet file of the first two
    conversations."""
    try:
        import pandas as pd
    except ImportError:
        print("SKIP the scripts whole: pandas is not installed", flush=True)
        return
    parquet = os.path.join(tmp, "corpus.parquet")
    pd.DataFrame(CORPUS[:2]).to_parquet(parquet)
    out = os.path.join(tmp, "built.json")
    benv = dict(env, BEAM_PARQUET=parquet, OUT=out, CONV="2", BEAM_STORE_FORMAT="whole_{n}")
    p = subprocess.run([sys.executable, os.path.join(HERE, "build_tasks.py")], env=benv, capture_output=True,
                       text=True, timeout=120)
    want = [t for row in CORPUS[:2] for t in build_tasks.tasks_of(row["conversation_id"],
                                                                   row["probing_questions"])[0]]
    check(p.returncode == 0 and json.load(open(out)) == want, "build_tasks.py whole" + (p.stderr[-400:] or ""))
    p = subprocess.run([sys.executable, os.path.join(HERE, "beam_ingest.py")], env=benv, capture_output=True,
                       text=True, timeout=120)
    check(p.returncode == 0 and [len(service.stores[f"whole_{n}"]["memories"]) for n in (1, 2)] == [4, 3],
          "beam_ingest.py whole" + ("" if p.returncode == 0 else f": {p.stdout[-400:]} {p.stderr[-400:]}"))
    p = subprocess.run([sys.executable, os.path.join(HERE, "verify_corpus.py")], env=benv, capture_output=True,
                       text=True, timeout=120)
    check(p.returncode == 0 and "no problems found" in p.stdout, "verify_corpus.py whole")


def check_run(check, reader, label, tasks, sdk, env, tmp, service, models):
    """One run.py pass with one reader, and what reached the service and the reader."""
    runs, reader_log = os.path.join(tmp, "runs"), os.path.join(tmp, "reader")
    start, mstart = len(service.log), len(models.log)
    p = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), label, "--reader", reader, "--tasks",
                        os.path.join(tmp, "tasks.json"), "--runs-dir", runs, "--conc", "2"],
                       env=env, capture_output=True, text=True, timeout=300)
    if not check(p.returncode == 0, f"{reader}: run.py finished" + ("" if p.returncode == 0 else
                                                                      f": {p.stdout[-600:]} {p.stderr[-800:]}")):
        return None
    out = os.path.join(runs, label)
    note, defs = sdk.note, sdk.definitions("openai" if reader == "openai" else "anthropic")

    leaked = []
    for root, _, files in os.walk(out):
        for fn in files:
            text = open(os.path.join(root, fn), errors="replace").read()
            if any(m in text for m in (GT_MARK, RUBRIC_MARK, KEY, READER_KEY)):
                leaked.append(fn)
    check(not leaked, f"{reader}: no reference answer, rubric or key in the run's files before grading {leaked or ''}")

    sent = service.log[start:]
    fmt = env.get("BEAM_STORE_FORMAT") or harness.DEFAULT_STORE_FORMAT
    check({r["path"] for r in sent} <= SDK_PATHS and sum(r["path"].endswith("/work/open") for r in sent) == 2
          and sorted(r["body"]["user_id"] for r in sent if r["path"].endswith("/work/open"))
          == sorted(fmt.format(n=t["user_id"].rsplit("-", 1)[-1]) for t in tasks),
          f"{reader}: only the memory tools' requests, one work per question, stores from {fmt}")
    check(all(r["headers"].get("x-wos-model") == "scroll-2" and r["headers"].get("x-api-key") == KEY
              and not any("preview" in h for h in r["headers"]) for r in sent),
          f"{reader}: every request on scroll-2 with the API key, no preview header")

    manifest = json.load(open(os.path.join(out, "manifest.json")))
    same = manifest["prompt"]["same_as_sdk_2_2_49"]
    check(manifest["sdk"]["memory_tools"]["preset"] == "S-2" and manifest["reader"]["kind"] == reader
          and manifest["service"]["model"] == "scroll-2" and manifest["questions"]["answered"] == len(tasks)
          and manifest["questions"]["turn_check_failed"] == [],
          f"{reader}: manifest (preset S-2, reader, model, every question answered)")
    check(same["reader_prompt"] and same["tools_note"] and same["tool_definitions"],
          f"{reader}: answer prompt, note and tool definitions of the SDK's S-2 = SDK 2.2.49")

    names = [d["name"] for d in sdk.definitions("anthropic")]
    for t in tasks:
        qid, plan = t["question_id"], PLANS[t["question"]]
        calls = calls_of(plan)
        row = json.load(open(os.path.join(out, "questions", f"{qid}.json")))
        prompt = harness.reader_prompt(row["first_page"], t["question"], note)
        check("gt" not in row and "rubric" not in row and row["answer"] == plan["answer"],
              f"{reader} {qid}: the reader's answer, and no gt or rubric field")
        check(all(c in row["first_page"] for c in plan["open"])
              and [c["name"] for c in row["tool_calls"]] == [c["name"] for c in calls]
              and [c["refused"] for c in row["tool_calls"]] == ["refuse" in c for c in calls]
              and all(x in c["text"] for c, want in zip(row["tool_calls"], calls)
                      for x in want.get("found", []) + want.get("pieces", []))
              and row["tools"] == {n: sum(1 for c in calls if c["name"] == n) for n in names},
              f"{reader} {qid}: first list and tool results as the SDK gave them")
        check(all(isinstance(x, float) for x in [row["begin_secs"], row["answered_secs"]]
                  + [c["secs"] for c in row["tool_calls"]]),
              f"{reader} {qid}: begin, each run and answered timed")
        if reader == "claude-cli":
            seen = json.load(open(os.path.join(reader_log, f"{qid}.json")))
            check_claude_cli(check, qid, seen, prompt, row, calls, defs, names, tmp)
        else:
            mine = [r for r in models.log[mstart:] if plan_for(r["body"]["messages"][0]["content"]) is plan]
            check_api_reader(check, reader, qid, mine, prompt, row, plan, defs)
    return out


def check_claude_cli(check, qid, seen, prompt, row, calls, defs, names, tmp):
    argv = seen["argv"]
    masked = [("<mcp>" if i and argv[i - 1] == "--mcp-config" else x) for i, x in enumerate(argv)]
    check(masked == harness.reader_args("claude", "claude-opus-5", "max", "<mcp>", names)[1:],
          f"claude-cli {qid}: reader flags, in the published order {masked}")
    check(argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
          and argv[argv.index("--allowedTools") + 1] == ",".join(f"mcp__m__{n}" for n in names),
          f"claude-cli {qid}: built-in tools off, strict MCP config, memory tools only")
    check(seen["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and seen["env"]["CLAUDE_CODE_DISABLE_ORG_MEMORY"] == "1"
          and not any(seen["env"][k] for k in ("WOS_API_KEY", "READER_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")),
          f"claude-cli {qid}: automatic memory off, no service, reader or judge key in its environment")
    check(seen["cwd_files"] == [] and not seen["cwd"].startswith(os.path.realpath(HERE))
          and not seen["cwd"].startswith(os.path.realpath(tmp)),
          f"claude-cli {qid}: reader works in an empty folder outside the harness and its runs")
    check(seen["prompt"] == prompt, f"claude-cli {qid}: prompt = answer template + first list + question + note")
    check(seen["tools"] == [{"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]}
                            for d in defs], f"claude-cli {qid}: tools over MCP = the SDK's definitions")
    check([r["content"][0]["text"] for r in seen["results"]] == [c["text"] for c in row["tool_calls"]]
          and not any(r["isError"] for r in seen["results"]), f"claude-cli {qid}: tool results = the SDK's text")
    check(row["turns_check"] == {"num_turns": len(calls) + 1, "tool_calls": len(calls), "ok": True},
          f"claude-cli {qid}: turn check {row['turns_check']}")


def check_api_reader(check, reader, qid, requests, prompt, row, plan, defs):
    first, last = requests[0], requests[-1]
    check(len(requests) == len(plan["turns"]) + 1 and row["turns"] == len(requests),
          f"{reader} {qid}: one request per turn, {len(requests)}")
    model = "claude-opus-5" if reader == "anthropic" else "selftest-model"
    check(all(r["body"]["messages"][0] == {"role": "user", "content": prompt} and r["body"]["tools"] == defs
              and r["body"]["model"] == model for r in requests),
          f"{reader} {qid}: the prompt as the user message, the SDK's {reader} definitions, the model")
    if reader == "anthropic":
        check(all(r["headers"].get("x-api-key") == READER_KEY and r["headers"].get("anthropic-version") == "2023-06-01"
                  and r["body"].get("output_config") == {"effort": "max"} for r in requests),
              f"anthropic {qid}: key, API version and effort sent")
        msgs = last["body"]["messages"]
        replies, answers = msgs[1::2], msgs[2::2]
        check(all(m["role"] == "assistant" and m["content"][0]["type"] == "thinking" for m in replies)
              and [len(m["content"]) for m in answers] == [len(t) for t in plan["turns"]],
              f"anthropic {qid}: each reply sent back whole, its tool calls answered in one message")
        results = [b for m in answers for b in m["content"]]
        check([b["content"] for b in results] == [c["text"] for c in row["tool_calls"]]
              and [b["tool_use_id"] for b in results]
              == [f"toolu_{i}_{j}" for i, t in enumerate(plan["turns"]) for j in range(len(t))],
              f"anthropic {qid}: tool results = the SDK's text, matched to their calls")
    else:
        check(all(r["headers"].get("authorization") == "Bearer " + READER_KEY for r in requests),
              f"openai {qid}: key sent")
        results = [m for m in last["body"]["messages"] if m["role"] == "tool"]
        check([m["content"] for m in results] == [c["text"] for c in row["tool_calls"]]
              and [m["tool_call_id"] for m in results]
              == [f"call_{i}_{j}" for i, t in enumerate(plan["turns"]) for j in range(len(t))],
              f"openai {qid}: tool results = the SDK's text, matched to their calls")
    check(row["reader_usage"] == ({"input_tokens": 10 * len(requests), "output_tokens": 5 * len(requests)}
                                  if reader == "anthropic" else
                                  {"prompt_tokens": 10 * len(requests), "completion_tokens": 5 * len(requests)}),
          f"{reader} {qid}: token counts summed over the turns")


def check_grading(check, env, tmp, labels, url):
    runs = os.path.join(tmp, "runs")
    genv = dict(env, OPENROUTER_API_KEY="selftest-judge-key", JUDGE_URL=url + "/judge/v1/chat/completions")
    scores = {}
    for label, split in [(labels[0], "raw")] + [(x, "trimmed") for x in labels]:
        g = subprocess.run([sys.executable, os.path.join(HERE, "grade.py"), label, "--tasks",
                            os.path.join(tmp, "tasks.json"), "--runs-dir", runs, "--eo-split", split],
                           env=genv, capture_output=True, text=True, timeout=300)
        if not check(g.returncode == 0, f"grade.py {label} --eo-split {split}"
                     + ("" if g.returncode == 0 else f": {g.stderr[-800:]}")):
            return
        out = os.path.join(runs, label)
        scores[label, split] = (json.load(open(os.path.join(out, "score.json"))),
                                json.load(open(os.path.join(out, "eo.json"))))
    graded = json.load(open(os.path.join(runs, labels[-1], "manifest.json")))["grading"]
    check(graded["eo_split"] == "trimmed" and set(graded["judge_models_returned"]) == {"selftest-judge"}
          and graded["rubric_items_unread"] == 0 and graded["equivalence_failed"] == {}
          and graded["judge_prompt"]["source"] == "scripts/beam_grade_gpt.py JUDGE",
          "grade: manifest entry (split, judge model returned, nothing unread or failed)")
    raw_eo = scores[labels[0], "raw"][1]["2_event_ordering_0"]
    check(raw_eo != 0.0, f"grade: event ordering, raw lines: the blank line is an item ({raw_eo:.3f})")
    for label in labels:
        score, eo = scores[label, "trimmed"]
        check(eo == {"2_event_ordering_0": 0.0} and score["score"] == 37.5 and score["score_rubric"] == 87.5,
              f"grade {label}: reversed order 0.0, official 37.5, rubric 87.5 "
              f"({eo}, {score['score']}, {score['score_rubric']})")
        an = subprocess.run([sys.executable, os.path.join(HERE, "analyze.py"), label, "--runs-dir", runs], env=env,
                            capture_output=True, text=True, timeout=120)
        table = [x for x in open(os.path.join(runs, label, "analysis.md"))
                 if x.startswith("| type |")] if an.returncode == 0 else []
        check(bool(table) and all(n in table[0] for n in ("deeper", "around", "search")),
              f"analyze {label}: one column per tool" + ("" if an.returncode == 0 else f": {an.stderr[-600:]}"))


def check_records(check, path, sdk):
    """Every per-question record in path rebuilt with this harness's prompt and the SDK's note."""
    note, defs = sdk.note, sdk.definitions("anthropic")
    check(hashlib.sha256(note.encode()).hexdigest() == harness.REFERENCE_1019["tools_note"]
          and hashlib.sha256(json.dumps(defs).encode()).hexdigest() == harness.REFERENCE_2_2_49["tool_definitions"],
          "the SDK's S-2 note = beam700_sdkoff1019, and its tool definitions = SDK 2.2.49")
    if harness.ENCODING is None:
        check(False, "token counts need tiktoken")
        return
    files = sorted(glob.glob(os.path.join(path, "*.json"))) if os.path.isdir(path) else [path]
    rows = [r for r in (json.load(open(f)) for f in files)
            if isinstance(r, dict) and r.get("question_id") and r.get("prompt_tokens") is not None]
    bad = [r["question_id"] for r in rows
           if harness.tokens(harness.reader_prompt(r.get("first_page", r.get("begin_text")), r["question"], note))
           != r["prompt_tokens"]]
    check(bool(rows) and not bad, f"{len(rows) - len(bad)}/{len(rows)} records: the rebuilt prompt has the "
                                  f"record's token count {bad[:5] or ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    check = Checks()
    try:
        import wontopos
    except ImportError:
        sys.exit("The wontopos SDK is missing: put it on the Python path.")
    sdk = wontopos.Client(api_key=KEY, model="scroll-2").memory_tools("selftest", preset=harness.PRESET)

    if a.record:
        check_records(check, a.record, sdk)
        return check.failed

    check_sources(check)
    tasks = check_tasks(check)
    service, models = Service(), Models()
    service_server, service_url = serve(service)
    models_server, models_url = serve(models)
    tmp = tempfile.mkdtemp(prefix="beam1m-selftest-")
    try:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("WOS_", "BEAM_", "READER_", "JUDGE_", "CLAUDE_"))
               and k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "COST_CAP")}
        claude = os.path.join(tmp, "claude")
        with open(claude, "w") as f:
            f.write(f"#!{sys.executable}\n" + FAKE_CLAUDE)
        os.chmod(claude, 0o755)
        reader_log = os.path.join(tmp, "reader")
        os.makedirs(reader_log)
        plan_path = os.path.join(tmp, "plan.json")
        json.dump({t["question_id"]: {"calls": calls_of(PLANS[t["question"]]), "answer": PLANS[t["question"]]["answer"]}
                   for t in tasks}, open(plan_path, "w"))
        json.dump(tasks, open(os.path.join(tmp, "tasks.json"), "w"))
        env.update(WOS_API_KEY=KEY, WOS_BASE_URL=service_url, CLAUDE_BIN=claude, SELFTEST_READER_LOG=reader_log,
                   SELFTEST_PLAN=plan_path)

        check_ingest(check, wontopos, service, service_url)
        check_whole_scripts(check, tmp, env, service)

        labels = []
        for reader, extra in [
                ("claude-cli", {}),
                ("anthropic", {"ANTHROPIC_API_KEY": READER_KEY, "READER_URL": models_url + "/v1/messages"}),
                ("openai", {"READER_KEY": READER_KEY, "READER_MODEL": "selftest-model",
                            "READER_URL": models_url + "/reader/v1/chat/completions",
                            "BEAM_STORE_FORMAT": "selftest_{n}"})]:
            label = f"selftest-{reader}"
            if check_run(check, reader, label, tasks, sdk, dict(env, **extra), tmp, service, models):
                labels.append(label)
        pre = json.load(open(os.path.join(reader_log, "__preflight__.json")))
        check(pre["argv"][pre["argv"].index("--output-format") + 1] == "stream-json" and "--verbose" in pre["argv"],
              "claude-cli: preflight read the reader's tool list")
        if labels:
            check_grading(check, env, tmp, labels, models_url)
    finally:
        service_server.shutdown()
        models_server.shutdown()
        if a.keep:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return check.failed


if __name__ == "__main__":
    failed = main()
    print(f"\n{'all checks passed' if not failed else f'{len(failed)} checks failed'}")
    sys.exit(1 if failed else 0)
