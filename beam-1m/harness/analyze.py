#!/usr/bin/env python3
"""What the reader did with the memory tools on a run, per question type and overall.

How often it went deeper, how it used around (the width it asked for, whether it moved the center), what it searched
for (its queries beside the question), tokens of memory text it was shown, the time of each SDK call (begin, each
tool call, answered), and the reader's time, turns and tokens (and cost, for claude-cli); once graded, the score
beside how much the reader leaned on the tools. SDK time is measured on this machine around the public call, so it
includes the network. The claude-cli cost is the CLI's total_cost_usd, the model's API list price for the tokens it
reports, not what a subscription is billed.
writes runs/<label>/analysis.md
usage: analyze.py <label>
"""
import argparse
import collections
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("label")
ap.add_argument("--runs-dir", default=os.path.join(HERE, "runs"))
a = ap.parse_args()
D = os.path.join(a.runs_dir, a.label)
QDIR = os.path.join(D, "questions")
rows = [json.load(open(os.path.join(QDIR, f))) for f in sorted(os.listdir(QDIR)) if f.endswith(".json")]
ok = [r for r in rows if not r.get("error") and not r.get("blocked") and (r.get("answer") or "").strip()]
sp = os.path.join(D, "score.json")
score = json.load(open(sp))["per_question"] if os.path.exists(sp) else {}
# The tools of this run, in the SDK's order: the table's header and its values come from this one list.
TOOLS = [d["name"] for d in json.load(open(os.path.join(D, "tool_definitions.json")))]


def mean(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(st.fmean(xs), nd) if xs else None


def med(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), nd) if xs else None


def p90(xs, nd=2):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, round(0.9 * (len(xs) - 1)))], nd) if xs else None


def calls_of(r):
    return sum(r["tools"].get(t, 0) for t in TOOLS)


def short(name):
    return name.replace("memory_", "")


def args(call):
    return call["input"] if isinstance(call.get("input"), dict) else {}


def output_tokens(r):
    u = r.get("reader_usage") or {}
    return u.get("output_tokens", u.get("completion_tokens"))


out = []
w = out.append
w(f"# BEAM-1M with the memory tools: {a.label}\n")
w(f"{len(rows)} questions run, {len(ok)} answered, {len(rows) - len(ok)} failed or blocked.")
checked = [r for r in ok if r.get("turns_check")]
if checked:
    bad = [r["question_id"] for r in checked if not r["turns_check"].get("ok")]
    w(f"Turn check (reader turns = tool calls + 1): {len(checked) - len(bad)}/{len(checked)} hold"
      + (f"; not on {', '.join(bad[:10])}" if bad else "") + ".\n")
if score:
    w(f"**Official BEAM score {100 * st.fmean(score.values()):.2f}** ({len(score)} graded).\n")

w("## Tool use per question\n")
header = ["type", "n", "score"] + [short(t) for t in TOOLS] + ["no tool call", "refused"]
w("| " + " | ".join(header) + " |")
w("|" + "---|" * len(header))
groups = collections.defaultdict(list)
for r in ok:
    groups[r["type"]].append(r)
for ty, rs in sorted(groups.items()) + [("ALL", ok)]:
    sc = mean([100 * score[r["question_id"]] for r in rs if r["question_id"] in score])
    cells = [ty, str(len(rs)), str(sc if sc is not None else "-")]
    cells += [str(mean([r["tools"].get(t, 0) for r in rs])) for t in TOOLS]
    cells += [str(sum(1 for r in rs if not calls_of(r))), str(sum(r.get("refused") or 0 for r in rs))]
    assert len(cells) == len(header)
    w("| " + " | ".join(cells) + " |")

if "memory_deeper" in TOOLS:
    w("\n## How many times it went deeper\n")
    dist = collections.Counter(r["tools"].get("memory_deeper", 0) for r in ok)
    w(" · ".join(f"{k} times: {dist[k]}" for k in sorted(dist)))
    if score:
        w("\nScore by deeper count: " + " · ".join(
            f"{k}: {mean([100 * score[r['question_id']] for r in ok if r['tools'].get('memory_deeper', 0) == k and r['question_id'] in score])}"
            for k in sorted(dist)))

w("\n## Around\n")
arounds = [c for r in ok for c in r["tool_calls"] if c["name"] == "memory_around"]
if arounds:
    widths = [(args(c).get("to", 3) or 0) - (args(c).get("from", -3) or 0) for c in arounds]
    moved = sum(1 for c in arounds if (args(c).get("center") or 0) != 0)
    w(f"{len(arounds)} calls in {sum(1 for r in ok if r['tools'].get('memory_around'))} questions. Width asked "
      f"(to - from): median {med(widths)}, p90 {p90(widths)}. Center moved off [n]: {moved}. "
      f"Tokens shown: median {med([c['tokens'] for c in arounds])}.")
else:
    w("No calls.")

w("\n## Search: where it looked again\n")
searches = [(r, c) for r in ok for c in r["tool_calls"] if c["name"] == "memory_search"]
if searches:
    w(f"{len(searches)} searches in {sum(1 for r in ok if r['tools'].get('memory_search'))} questions; "
      f"rooms named in {sum(1 for _, c in searches if args(c).get('rooms'))}. Tokens shown: median "
      f"{med([c['tokens'] for _, c in searches])}. Queries beside their questions (first 40):\n")
    for r, c in searches[:40]:
        w(f"- [{r['type']}] Q: {r['question'][:110]}\n  → search: {str(args(c).get('query', ''))[:140]}")
else:
    w("No searches.")

w("\n## Tokens and time per tool\n")
w("| SDK call | calls | tokens shown (median) | seconds (median) | seconds (p90) |")
w("|---|---|---|---|---|")
w(f"| begin (first list) | {len(ok)} | {med([r['first_page_tokens'] for r in ok])} | "
  f"{med([r.get('begin_secs') for r in ok], 3)} | {p90([r.get('begin_secs') for r in ok], 3)} |")
for t in TOOLS:
    cs = [c for r in ok for c in r["tool_calls"] if c["name"] == t]
    if cs:
        w(f"| run {t} | {len(cs)} | {med([c['tokens'] for c in cs])} | {med([c['secs'] for c in cs], 3)} | "
          f"{p90([c['secs'] for c in cs], 3)} |")
w(f"| answered | {len(ok)} | - | {med([r.get('answered_secs') for r in ok], 3)} | "
  f"{p90([r.get('answered_secs') for r in ok], 3)} |")

w("\n## Per question\n")
w("| | median | mean | p90 |")
w("|---|---|---|---|")
for name, f, nd in [("memory tokens shown", lambda r: r["memory_tokens_shown"], 0),
                    ("SDK calls", lambda r: len(r["tool_calls"]) + 2, 1),
                    ("SDK seconds", lambda r: r["sdk_secs"], 2),
                    ("reader seconds", lambda r: r.get("reader_secs"), 1),
                    ("reader turns", lambda r: r.get("num_turns", r.get("turns")), 1),
                    ("reader output tokens", output_tokens, 0),
                    ("reader cost_usd (API list price)", lambda r: r.get("cost_usd"), 4)]:
    xs = [f(r) for r in ok]
    if any(x is not None for x in xs):
        w(f"| {name} | {med(xs, nd)} | {mean(xs, nd)} | {p90(xs, nd)} |")
if any(r.get("cost_usd") is not None for r in ok):
    w(f"\nReader cost_usd in all: ${sum(r.get('cost_usd') or 0 for r in ok):.2f} for {len(ok)} questions.")
if score:
    w("\n## Score against tool use\n")
    for name, sel in [("no tool call", lambda r: not calls_of(r)),
                      ("1-2 calls", lambda r: 1 <= calls_of(r) <= 2),
                      ("3+ calls", lambda r: calls_of(r) >= 3)]:
        rs = [r for r in ok if sel(r) and r["question_id"] in score]
        w(f"- {name}: {len(rs)} questions, score {mean([100 * score[r['question_id']] for r in rs])}")
text = "\n".join(out) + "\n"
with open(os.path.join(D, "analysis.md"), "w") as fh:
    fh.write(text)
print(text)
