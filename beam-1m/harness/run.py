#!/usr/bin/env python3
"""BEAM-1M with the memory tools: the reader answers from the first list of memories and may look further.

Everything asked of the memory service goes through the public SDK. For each question
`client.memory_tools(store, preset="S-2")` makes the memory tools with every other switch at its default (lines
without speakers or dates, no photos, three tools); `begin(question)` gives the first list, `run(call)` answers
each tool call, and `answered()` closes the question once the reader has answered. Each of the three is timed
here, from outside the call.

The first list (20 memories) goes into the answer prompt and the SDK's note under TOOLS. The reader is one of:

  claude-cli  `claude -p` with every built-in tool off and only the memory tools, served over MCP by
              mcp_proxy.py. The condition of the published memory-tools runs.
  anthropic   the Messages API: the same prompt as the user message, the tools from definitions("anthropic").
  openai      any OpenAI-compatible chat completions endpoint: the same, with definitions("openai").

The two API readers run a loop here: every tool call in a reply is answered with run(call) and sent back, until
a reply has no tool call or --max-turns requests have been made.

Kept per question in runs/<label>/questions/<qid>.json: the answer, the first list, every tool call with its
input, result and time, the time of begin and answered, and the reader's turns, tokens and time (and, for
claude-cli, its cost). The reference answer and the rubric are never written here; grade.py joins them from the
task file.

With claude-cli, the reader is started once before the first question to read the tool list it reports:
anything besides the memory tools stops the run. After each question, num_turns is checked against the tool
calls made (one turn per call, plus the answer).

env
  WOS_API_KEY        API key (required)
  WOS_BASE_URL       the service address; unset, the SDK's default
  BEAM_STORE_FORMAT  store id of a conversation, default beam1m_{n} ({n} is the conversation number)
  BEAM_TASKS         task file, as built by build_tasks.py (or --tasks)
  READER_MODEL       claude-cli and anthropic: default claude-opus-5; openai: required
  READER_EFFORT      claude-cli and anthropic: default max
  CLAUDE_BIN         claude-cli: default claude
  COST_CAP           claude-cli: stop once its reported cost for the label passes this, in dollars (default 250)
  ANTHROPIC_API_KEY  anthropic: the key
  READER_URL         anthropic: default https://api.anthropic.com/v1/messages
                     openai: default https://api.openai.com/v1/chat/completions
  READER_KEY         openai: the key
  READER_MAX_TOKENS  anthropic: output budget of one request, default 32000

usage: run.py <label> [--reader claude-cli|anthropic|openai] [--tasks FILE] [--qids ID,ID | --limit N]
              [--conc 6] [--max-turns 16]
A label that already has answers continues: questions answered without error are not asked again.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "mcp_proxy.py")
MODEL = "scroll-2"
# The memory tools' preset: everything the SDK decides about them, and the model they run on.
PRESET = "S-2"
DEFAULT_STORE_FORMAT = "beam1m_{n}"
READERS = ("claude-cli", "anthropic", "openai")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
# The published runs used 6. A store holds at most 8 open works, so above 8 the questions of one conversation could
# push each other's works out.
MAX_CONC = 6
READER_TIMEOUT = 2400

# The answer prompt of the published memory-tools runs (30 September and 18-19 October 2026). It is not BEAM's own
# answer prompt: it adds "If the memories do not contain the answer, say so." and has no "without any explanation".
READER_PROMPT = """Answer the question using only the memories below.
Do not use outside knowledge. If the memories do not contain the answer, say so.

MEMORIES
{memories}

QUESTION
{question}

Answer concisely."""

# sha256 of what the reader was given in beam700_sdkoff1019 (19 October 2026). tool_definitions is the sha256 of
# json.dumps(definitions("anthropic")) with Python's defaults, the bytes of tool_definitions.json. The task hashes
# are over the canonical JSON (sorted keys, no spaces) of each question's id, user_id, type and question, and of its
# rubric.
REFERENCE_1019 = {
    "reader_prompt": "b6a334099a93ad360558dff781fcffa920dbbbade637247c0daa4b2d7cbe56e9",
    "tools_note": "cd69cb265fdad94b79707eba6fdada84c21d89aadaa789b866f3a297ae36908c",
    "tool_definitions": "697c686f0212b9fb96dd32e8a526bfa5955085a34690caeed6bab4069480f732",
    "task_questions": "578460c0186bb3c364f7cfc6308e1128364c658ac3fd10e649d7d1d58b1e6564",
    "task_rubrics": "c780151383791126fdbcf4d15de2a89f7bdc331cb8d4d26668feb0b4a6262214",
}
# SDK 2.2.49, the release: the same, but for the wording of memory_search's description.
REFERENCE_2_2_49 = dict(REFERENCE_1019, tool_definitions="55f3ffac75eee49e31761ff3a76d622d2ebef7847411e0c2309f0f28a7a2ab10")

# A closed subscription window or a lost login is "stop", not an answer. Read only where the CLI speaks, never
# inside an answer: the CLI's own error flag, a short reply that opens with one of its messages, or its stderr.
LIMIT_REPLY = re.compile(r"\s*(claude ai usage limit reached|you've hit your (session|usage|weekly) limit|"
                         r"not logged in|please run /login|invalid api key|api error|credit balance is too low)", re.I)
LIMIT_STDERR = re.compile(r"usage limit|hit your (session|usage|weekly) limit|not logged in|please run /login|"
                          r"invalid api key|credit balance is too low", re.I)
# Keys the claude-cli reader has no use for; they are not passed to it.
NOT_FOR_READER = ("WOS_API_KEY", "WONTOPOS_API_KEY", "READER_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")

# Token counts use o200k_base when tiktoken is installed, and are left empty when it is not: a count is never
# estimated.
try:
    import tiktoken
    ENCODING = tiktoken.get_encoding("o200k_base")
except ImportError:
    ENCODING = None


def tokens(text):
    if ENCODING is None:
        return None
    return len(ENCODING.encode(text, disallowed_special=())) if text else 0


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj):
    return sha256_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path, obj, indent=1):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def reader_prompt(first, question, note):
    """What every reader is given: the answer prompt holding the first list, then the SDK's note under TOOLS."""
    return READER_PROMPT.format(memories=first or "(none)", question=question) + "\n\nTOOLS\n" + note


def shown_input(call):
    """A tool call's arguments as recorded: the input object, or an OpenAI call's JSON string read as one."""
    if call.get("input") is not None:
        return call["input"]
    try:
        return json.loads(call.get("arguments") or "{}")
    except (TypeError, ValueError):
        return call.get("arguments")


class Questions:
    """One SDK MemoryTools per question, as an app holds one per turn, and every call made to it, timed around the
    call."""

    def __init__(self, client):
        self.client = client
        self.open = {}
        self.calls = {}
        self.lock = threading.Lock()

    def timed(self, qid, rec, fn):
        t0 = time.perf_counter()
        text, err = "", None
        try:
            text = fn()
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        rec.update(secs=round(time.perf_counter() - t0, 3), text=text, tokens=tokens(text),
                   refused=text.startswith("Refused"), error=err, at=utc_now())
        with self.lock:
            self.calls[qid].append(rec)
        if err:
            raise RuntimeError(err)
        return text

    def begin(self, qid, store, question):
        mem = self.client.memory_tools(store, preset=PRESET)
        with self.lock:
            self.open[qid] = mem
            self.calls[qid] = []
        return self.timed(qid, {"op": "begin"}, lambda: mem.begin(question))

    def run(self, qid, call):
        """call: {"name", "input"}, or {"name", "arguments"} from an OpenAI-style reader; the SDK reads either."""
        return self.timed(qid, {"op": "run", "name": call.get("name"), "input": shown_input(call)},
                          lambda: self.open[qid].run(call))

    def answered(self, qid):
        with self.lock:
            mem = self.open.pop(qid, None)
        if mem is None:
            return

        def close():
            mem.answered()
            return ""
        self.timed(qid, {"op": "answered"}, close)

    def take_calls(self, qid):
        with self.lock:
            return self.calls.pop(qid, [])


# ── claude-cli ────────────────────────────────────────────────────────────────

def serve_tools(questions, token):
    """The reader's tool calls reach the harness here, from mcp_proxy.py, on the loopback interface only."""

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/run" or not secrets.compare_digest(self.headers.get("X-Harness-Token") or "", token):
                return self.reply(404, {"error": "unknown path"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            try:
                text = questions.run(body.get("qid"), {"name": body.get("name"), "input": body.get("input") or {}})
                self.reply(200, {"text": text})
            except Exception as e:
                self.reply(500, {"error": f"{type(e).__name__}: {str(e)[:300]}"})

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reader_args(claude_bin, model, effort, mcp_config, names, output="json"):
    # The flags of the published runs, in their order: no settings but local ones, no MCP server but ours, every
    # built-in tool off, nothing kept between questions, only the memory tools allowed.
    args = [claude_bin, "-p", "--model", model, "--effort", effort, "--setting-sources", "local",
            "--strict-mcp-config", "--tools", "", "--output-format", output, "--no-session-persistence",
            "--mcp-config", mcp_config, "--allowedTools", ",".join(f"mcp__m__{n}" for n in names)]
    if output == "stream-json":
        args.append("--verbose")
    return args


def reader_env():
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", CLAUDE_CODE_DISABLE_ORG_MEMORY="1")
    for k in NOT_FOR_READER:
        env.pop(k, None)
    return env


def preflight(claude_bin, model, effort, mcp_config, names, cwd):
    """Start the reader once and read the tools it reports before its first turn. Stops the run unless they are
    exactly the memory tools and the memory server is connected."""
    errf = tempfile.TemporaryFile(mode="w+")
    p = subprocess.Popen(reader_args(claude_bin, model, effort, mcp_config, names, "stream-json"),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True,
                         cwd=cwd, env=reader_env())
    timer = threading.Timer(180, p.kill)
    timer.start()
    init = None
    try:
        p.stdin.write("Reply with OK.")
        p.stdin.close()
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                init = ev
                break
    finally:
        timer.cancel()
        p.kill()
        p.wait()
        p.stdout.close()
        errf.seek(0)
        err = errf.read()
        errf.close()
    if init is None:
        sys.exit(f"The reader reported no tool list before its first turn. stderr: {(err or '')[-400:]}")
    want = sorted(f"mcp__m__{n}" for n in names)
    got = sorted(init.get("tools") or [])
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or [] if isinstance(s, dict)}
    if got != want or servers.get("m") != "connected":
        sys.exit(f"The reader's tools are not only the memory tools: {got} (servers {servers}). Stopping.")
    return {"claude_code_version": init.get("claude_code_version"), "model": init.get("model"),
            "api_key_source": init.get("apiKeySource"), "permission_mode": init.get("permissionMode"),
            "tools": got, "mcp_servers": servers}


def ask_claude_cli(prompt, mcp_config, cfg):
    p = subprocess.run(reader_args(cfg.claude_bin, cfg.model, cfg.effort, mcp_config, cfg.names),
                       input=prompt, capture_output=True, text=True, cwd=cfg.cwd, env=reader_env(),
                       timeout=READER_TIMEOUT)
    if p.returncode and not p.stdout.strip().startswith("{"):
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    res = json.loads(p.stdout)
    out = {"answer": res.get("result"), "is_error": res.get("is_error"), "num_turns": res.get("num_turns"),
           "duration_ms": res.get("duration_ms"), "cost_usd": res.get("total_cost_usd"),
           "reader_usage": res.get("usage"), "reader_models": sorted((res.get("modelUsage") or {}).keys())}
    result = str(res.get("result") or "")
    if res.get("is_error") or (len(result) < 300 and LIMIT_REPLY.match(result)):
        out["blocked"] = result[:300]
    return out


# ── anthropic and openai ──────────────────────────────────────────────────────

class Blocked(Exception):
    """The reader cannot answer at all (a key refused, no credit): stop the run rather than record errors."""


def post_json(url, body, headers, timeout):
    """POST a JSON body and read the JSON reply. Rate limits and server errors are tried again, four times in all."""
    data = json.dumps(body).encode()
    for i in range(4):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers=dict(headers, **{"content-type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
            if e.code in (401, 402, 403):
                raise Blocked(detail)
            if e.code not in (408, 429, 500, 502, 503, 504, 529) or i == 3:
                raise RuntimeError(detail)
        time.sleep(min(60, 5 * 2 ** i))


def add_usage(total, usage):
    """Sum a reply's token counts into total, field by field, as the provider names them."""
    for k, v in (usage or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


def tool_reply(run_tool, call):
    """The text for one tool call. A call that fails is told to the reader as failed, as the MCP server does."""
    try:
        return run_tool(call), False
    except Exception as e:
        return f"Tool failed: {e}", True


def ask_anthropic(prompt, run_tool, cfg):
    headers = {"x-api-key": cfg.key, "anthropic-version": "2023-06-01"}
    messages = [{"role": "user", "content": prompt}]
    usage, models = {}, set()
    for turn in range(1, cfg.max_turns + 1):
        body = {"model": cfg.model, "max_tokens": cfg.max_tokens, "messages": messages, "tools": cfg.tools}
        if cfg.effort:
            body["output_config"] = {"effort": cfg.effort}
        d = post_json(cfg.url, body, headers, READER_TIMEOUT)
        add_usage(usage, d.get("usage"))
        models.add(d.get("model"))
        content = d.get("content") or []
        uses = [b for b in content if b.get("type") == "tool_use"]
        if d.get("stop_reason") != "tool_use" or not uses:
            answer = "\n".join(b.get("text") or "" for b in content if b.get("type") == "text").strip()
            return {"answer": answer, "turns": turn, "stop_reason": d.get("stop_reason"), "reader_usage": usage,
                    "reader_models": sorted(m for m in models if m)}
        # The reply goes back whole, thinking included, and every tool call in it is answered in one message.
        messages.append({"role": "assistant", "content": content})
        results = []
        for b in uses:
            text, failed = tool_reply(run_tool, {"name": b.get("name"), "input": b.get("input") or {}})
            results.append({"type": "tool_result", "tool_use_id": b.get("id"), "content": text, "is_error": failed})
        messages.append({"role": "user", "content": results})
    raise RuntimeError(f"no answer within {cfg.max_turns} turns")


def ask_openai(prompt, run_tool, cfg):
    headers = {"authorization": "Bearer " + cfg.key}
    messages = [{"role": "user", "content": prompt}]
    usage, models = {}, set()
    for turn in range(1, cfg.max_turns + 1):
        d = post_json(cfg.url, {"model": cfg.model, "messages": messages, "tools": cfg.tools}, headers,
                      READER_TIMEOUT)
        add_usage(usage, d.get("usage"))
        models.add(d.get("model"))
        choice = d["choices"][0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": (msg.get("content") or "").strip(), "turns": turn,
                    "stop_reason": choice.get("finish_reason"), "reader_usage": usage,
                    "reader_models": sorted(m for m in models if m)}
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            text, _ = tool_reply(run_tool, c.get("function") or {})
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": text})
    raise RuntimeError(f"no answer within {cfg.max_turns} turns")


def reader_config(a):
    """The reader's settings, from the environment. Stops when one it needs is missing."""
    env = os.environ.get
    cfg = types.SimpleNamespace(kind=a.reader, max_turns=a.max_turns, model=env("READER_MODEL"), effort=None)
    if a.reader == "openai":
        if not cfg.model:
            sys.exit("Set READER_MODEL for the openai reader.")
        cfg.url, cfg.key = env("READER_URL") or OPENAI_URL, env("READER_KEY")
        if not cfg.key:
            sys.exit("Set READER_KEY for the openai reader.")
        return cfg
    cfg.model = cfg.model or "claude-opus-5"
    cfg.effort = env("READER_EFFORT") or "max"
    if a.reader == "claude-cli":
        cfg.claude_bin = env("CLAUDE_BIN") or "claude"
        return cfg
    cfg.url, cfg.key = env("READER_URL") or ANTHROPIC_URL, env("ANTHROPIC_API_KEY")
    cfg.max_tokens = int(env("READER_MAX_TOKENS") or 32000)
    if not cfg.key:
        sys.exit("Set ANTHROPIC_API_KEY for the anthropic reader.")
    return cfg


# ── the run ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BEAM-1M with the memory tools.")
    ap.add_argument("label")
    ap.add_argument("--reader", choices=READERS, default="claude-cli")
    ap.add_argument("--tasks", default=os.environ.get("BEAM_TASKS"))
    ap.add_argument("--qids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--conc", type=int, default=MAX_CONC)
    ap.add_argument("--max-turns", type=int, default=16, help="anthropic and openai: requests per question at most")
    ap.add_argument("--runs-dir", default=os.path.join(HERE, "runs"))
    a = ap.parse_args()
    if not a.tasks:
        sys.exit("Name the task file: --tasks or BEAM_TASKS.")
    if not 1 <= a.conc <= MAX_CONC:
        sys.exit(f"--conc must be 1 to {MAX_CONC}, the concurrency the published runs used at most.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a.label):
        sys.exit("The label may hold letters, digits, '.', '_' and '-'.")

    api_key = os.environ.get("WOS_API_KEY", "").strip()
    if not api_key:
        sys.exit("Set WOS_API_KEY.")
    base_url = os.environ.get("WOS_BASE_URL", "").strip()
    store_format = os.environ.get("BEAM_STORE_FORMAT", "").strip() or DEFAULT_STORE_FORMAT
    if "{n}" not in store_format:
        sys.exit("BEAM_STORE_FORMAT must hold {n}, the conversation number.")
    cfg = reader_config(a)
    cost_cap = float(os.environ.get("COST_CAP", "250"))

    try:
        import wontopos
    except ImportError:
        sys.exit("The wontopos SDK is missing: pip install 'wontopos>=2.2.49'")
    client = wontopos.Client(api_key=api_key, model=MODEL, timeout=180, retries=1,
                             **({"base_url": base_url} if base_url else {}))
    questions = Questions(client)

    all_tasks = json.load(open(a.tasks))
    tasks = all_tasks
    if a.qids:
        by_id = {t["question_id"]: t for t in all_tasks}
        missing = [q for q in a.qids.split(",") if q not in by_id]
        if missing:
            sys.exit(f"Not in the task file: {missing[:5]}")
        tasks = [by_id[q] for q in a.qids.split(",")]
    elif a.limit:
        tasks = all_tasks[:a.limit]

    def store_of(t):
        return store_format.format(n=t["user_id"].rsplit("-", 1)[-1])

    # The note and the definitions ask the service nothing; any store gives the same ones.
    try:
        probe = client.memory_tools(store_of(tasks[0]), preset=PRESET)
    except TypeError:
        sys.exit("This wontopos SDK takes no memory tools preset: install 2.2.49 or later.")
    note, defs = probe.note, probe.definitions("anthropic")
    names = [d["name"] for d in defs]
    defs_text = json.dumps(defs)
    cfg.names = names
    cfg.tools = probe.definitions("openai") if a.reader == "openai" else defs

    out = os.path.join(a.runs_dir, a.label)
    qdir = os.path.join(out, "questions")
    os.makedirs(qdir, exist_ok=True)
    defs_path = os.path.join(out, "tool_definitions.json")
    with open(defs_path, "w") as f:
        f.write(defs_text)

    reader = {"kind": a.reader, "model": cfg.model, "effort": cfg.effort, "concurrency": a.conc,
              "timeout_s": READER_TIMEOUT}
    server = work = mcp_config = None
    if a.reader == "claude-cli":
        work = tempfile.mkdtemp(prefix="beam1m-harness-")
        # The reader works in an empty folder outside the harness and its runs, the same one for every question.
        cfg.cwd = tempfile.mkdtemp(prefix="beam1m-reader-")
        token = secrets.token_hex(16)
        server = serve_tools(questions, token)
        tools_url = f"http://127.0.0.1:{server.server_address[1]}"

        def mcp_config(qid):
            path = os.path.join(work, f"{qid}.mcp.json")
            with open(path, "w") as f:
                json.dump({"mcpServers": {"m": {"command": sys.executable, "args": [PROXY],
                                                "env": {"QID": qid, "TOOLS_URL": tools_url, "TOOLS_TOKEN": token,
                                                        "DEFS": defs_path}}}}, f)
            return path

        cli_version = subprocess.run([cfg.claude_bin, "--version"], capture_output=True, text=True).stdout.strip()
        init = preflight(cfg.claude_bin, cfg.model, cfg.effort, mcp_config("__preflight__"), names, cfg.cwd)
        print(f"reader: {cli_version}, tools {init['tools']}", flush=True)
        reader.update(cli_version=cli_version, preflight=init,
                      flags=reader_args("claude", cfg.model, cfg.effort, "<mcp config>", names)[1:],
                      env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1"},
                      cwd="an empty temporary folder outside the harness, one per run")
    else:
        default_url = ANTHROPIC_URL if a.reader == "anthropic" else OPENAI_URL
        reader.update(endpoint="default" if cfg.url == default_url else "custom", max_turns=a.max_turns,
                      tool_definitions_sha256=sha256_text(json.dumps(cfg.tools)))
        if a.reader == "anthropic":
            reader.update(api_version="2023-06-01", max_tokens=cfg.max_tokens)
    reader_label = " ".join(x for x in (a.reader, cfg.model, cfg.effort) if x)

    hashes = {"reader_prompt": sha256_text(READER_PROMPT), "tools_note": sha256_text(note),
              "tool_definitions": sha256_text(defs_text),
              "task_questions": canonical_sha256([{k: t.get(k) for k in ("question_id", "user_id", "type", "question")}
                                                  for t in all_tasks]),
              "task_rubrics": canonical_sha256([{"question_id": t["question_id"], "rubric": t.get("rubric")}
                                                for t in all_tasks])}
    same = {k: hashes[k] == REFERENCE_1019[k] for k in REFERENCE_1019}
    for k, ok in same.items():
        if not ok:
            print(f"note: {k} differs from beam700_sdkoff1019", flush=True)
    manifest_path = os.path.join(out, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    manifest.update({
        "label": a.label,
        "benchmark": "BEAM-1M",
        "harness_sha256": {f: sha256_file(os.path.join(HERE, f)) for f in ("run.py", "mcp_proxy.py")},
        "service": {"base_url": "custom" if base_url else "default", "model": MODEL,
                    "store_format": "default" if store_format == DEFAULT_STORE_FORMAT else "custom"},
        "sdk": {"version": getattr(wontopos, "__version__", None),
                "module_sha256": sha256_file(wontopos.__file__) if getattr(wontopos, "__file__", None) else None,
                "memory_tools": {"preset": probe.preset, "asked": {"preset": PRESET}, "switches": "SDK defaults"},
                "tools": names, "timeout_s": 180, "retries": 1},
        "reader": reader,
        "tokens": "o200k_base" if ENCODING else "not counted (tiktoken not installed)",
        "prompt": {"name": "default", "sha256": hashes, "same_as_beam700_sdkoff1019": same,
                   "same_as_sdk_2_2_49": {k: hashes[k] == REFERENCE_2_2_49[k] for k in REFERENCE_2_2_49}},
        "tasks": {"file_md5": hashlib.md5(open(a.tasks, "rb").read()).hexdigest(), "questions": len(all_tasks),
                  "selected": len(tasks)},
    })
    manifest.setdefault("runs", []).append({"started_at": utc_now(), "selected": len(tasks), "reader": a.reader})
    write_json(manifest_path, manifest)

    stop = threading.Event()
    lock = threading.Lock()

    def finished(qid):
        path = os.path.join(qdir, f"{qid}.json")
        try:
            r = json.load(open(path))
        except (OSError, ValueError):
            return False
        return not r.get("error") and not r.get("blocked") and bool((r.get("answer") or "").strip())

    def spent_so_far():
        total = 0.0
        for f in os.listdir(qdir):
            if f.endswith(".json"):
                try:
                    total += json.load(open(os.path.join(qdir, f))).get("cost_usd") or 0.0
                except (OSError, ValueError):
                    pass
        return total

    spent = [spent_so_far()]

    def one(t):
        if stop.is_set():
            return None
        qid = t["question_id"]
        row = {"question_id": qid, "user_id": t["user_id"], "type": t["type"], "question": t["question"],
               "reader": reader_label}
        t0 = time.time()
        try:
            first = questions.begin(qid, store_of(t), t["question"])
            prompt = reader_prompt(first, t["question"], note)
            row.update(prompt_tokens=tokens(prompt), prompt_sha256=sha256_text(prompt))
            r0 = time.time()
            if a.reader == "claude-cli":
                row.update(ask_claude_cli(prompt, mcp_config(qid), cfg))
            elif a.reader == "anthropic":
                row.update(ask_anthropic(prompt, lambda call: questions.run(qid, call), cfg))
            else:
                row.update(ask_openai(prompt, lambda call: questions.run(qid, call), cfg))
            row["reader_secs"] = round(time.time() - r0, 1)
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:400]}"
            if isinstance(e, Blocked) or LIMIT_STDERR.search(row["error"]):
                row["blocked"] = row["error"][:300]
        finally:
            try:
                questions.answered(qid)
            except Exception as e:
                row["close_error"] = str(e)[:200]
        if row.get("blocked"):
            stop.set()
        calls = questions.take_calls(qid)
        begin = next((c for c in calls if c["op"] == "begin"), {})
        closing = next((c for c in calls if c["op"] == "answered"), {})
        runs = [c for c in calls if c["op"] == "run"]
        shown = [c.get("tokens") for c in calls if c["op"] in ("begin", "run")]
        row.update(
            first_page=begin.get("text"),
            first_page_tokens=begin.get("tokens"),
            tools={n: sum(1 for c in runs if c.get("name") == n) for n in names},
            refused=sum(1 for c in runs if c.get("refused")),
            tool_calls=[{k: c.get(k) for k in ("name", "input", "tokens", "secs", "refused", "error", "text")}
                        for c in runs],
            memory_tokens_shown=None if None in shown else sum(shown),
            begin_secs=begin.get("secs"),
            answered_secs=closing.get("secs"),
            sdk_secs=round(sum(c.get("secs") or 0 for c in calls), 3),
            total_secs=round(time.time() - t0, 1))
        if a.reader == "claude-cli":
            row["turns_check"] = {"num_turns": row.get("num_turns"), "tool_calls": len(runs),
                                  "ok": row.get("num_turns") == len(runs) + 1}
        write_json(os.path.join(qdir, f"{qid}.json"), row)
        with lock:
            print(json.dumps({k: row.get(k) for k in ("question_id", "tools", "memory_tokens_shown", "sdk_secs",
                                                      "reader_secs", "num_turns", "turns", "cost_usd", "error",
                                                      "blocked")}, ensure_ascii=False), flush=True)
            spent[0] += row.get("cost_usd") or 0.0
            if spent[0] > cost_cap:
                stop.set()
        return row

    todo = [t for t in tasks if not finished(t["question_id"])]
    print(f"{len(tasks)} questions, {len(tasks) - len(todo)} already answered, {len(todo)} to go, reader {a.reader}, "
          f"conc {a.conc}" + (f", reader cost so far ${spent[0]:.2f} (cap ${cost_cap:.0f})"
                               if a.reader == "claude-cli" else ""), flush=True)
    t_start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=a.conc) as ex:
            for n, _ in enumerate(ex.map(one, todo), 1):
                if n % 25 == 0:
                    with lock:
                        print(f"  {n}/{len(todo)} ({(time.time() - t_start) / 60:.1f} min)", flush=True)
    finally:
        if server:
            server.shutdown()
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(cfg.cwd, ignore_errors=True)

    rows = []
    for t in tasks:
        path = os.path.join(qdir, f"{t['question_id']}.json")
        if os.path.exists(path):
            rows.append(json.load(open(path)))
    write_json(os.path.join(out, "answers.json"), {"label": a.label, "reader": reader_label, "rows": rows},
               indent=None)
    answered = [r for r in rows if not r.get("error") and not r.get("blocked") and (r.get("answer") or "").strip()]
    usage = {}
    for r in rows:
        add_usage(usage, r.get("reader_usage"))
    manifest = json.load(open(manifest_path))
    manifest["runs"][-1].update(finished_at=utc_now(), stopped=stop.is_set())
    manifest["questions"] = {
        "selected": len(tasks), "answered": len(answered),
        "errors": sum(1 for r in rows if r.get("error")), "blocked": sum(1 for r in rows if r.get("blocked")),
        "turn_check_failed": sorted(r["question_id"] for r in answered
                                    if not (r.get("turns_check") or {"ok": True}).get("ok")),
        "reader_models": sorted({m for r in rows for m in r.get("reader_models") or []}),
        "reader_usage": usage,
        "reader_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in rows), 2) if a.reader == "claude-cli" else None,
    }
    write_json(manifest_path, manifest)
    left = len(tasks) - len(answered)
    failed = manifest["questions"]["turn_check_failed"]
    print(f"{'STOPPED' if stop.is_set() else 'done'}: {len(answered)}/{len(tasks)} answered, "
          f"{(time.time() - t_start) / 60:.1f} min" + (f", {left} left" if left else "")
          + (f"; turn check failed on {len(failed)}" if failed else ""), flush=True)


if __name__ == "__main__":
    main()
