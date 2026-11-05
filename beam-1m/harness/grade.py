#!/usr/bin/env python3
"""Grade a run of run.py the way the published BEAM-1M runs were graded.

Rubric: the judge scores each rubric item 1, 0.5 or 0 with the judge prompt in ../scripts/beam_grade_gpt.py (read
from that file at start, the prompt the published runs were graded with); a question's score is the mean of its
items, 0.5 counted as 0.5. An item whose verdict cannot be read is left out of that mean, not counted as 0.

Event ordering: the answer is cut into lines, each line is matched to a rubric item by the equivalence prompt in
../scripts/beam_eo.py (an item matched once is not used again), and the score is (tau_b + 1) / 2 over the matched
order, as BEAM's compute_metrics.py and report_results.py do (tau_b is computed here in plain Python, the value
scipy's kendalltau(variant="b") gives). Lines: by default each line is trimmed and blank lines are dropped, as the
published runs were graded (--eo-split trimmed). BEAM's own code cuts on newlines only, so a blank line becomes an
item; --eo-split raw does that. An equivalence call that fails four times counts as "no", as in the published runs;
how many failed is kept in manifest.json, and a question with any should be graded again.

Official score: event ordering by tau, every other type by rubric, mean over questions. Rubric score: every type
by rubric. Reference answers and rubrics come from the task file here, at grading, and nowhere earlier.

env: OPENROUTER_API_KEY (judge openai/gpt-4.1-mini through OpenRouter, as the published runs) or OPENAI_API_KEY
     (gpt-4.1-mini directly); JUDGE_MODEL names another model, JUDGE_URL another OpenAI-compatible endpoint.
writes runs/<label>/beam.json (rubric verdicts with reasons), eo.json (tau per event-ordering question),
       score.json, and a "grading" entry in manifest.json
usage: grade.py <label> [--tasks FILE] [--eo-split trimmed|raw] [--conc 8]
"""
import argparse
import ast
import collections
import hashlib
import json
import math
import os
import re
import statistics as st
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
RUBRIC_SCRIPT = os.path.join(SCRIPTS, "beam_grade_gpt.py")
EO_SCRIPT = os.path.join(SCRIPTS, "beam_eo.py")
# The equivalence call's user message, as beam_eo.py builds it (checked against that file below).
EQ_USER = "First snippet: %s \n\n Second snippet: %s"
EQ_USER_SOURCE = '"First snippet: %s \\n\\n Second snippet: %s"'


def constant(path, name):
    """A module-level string constant of a script, read without running the script."""
    src = open(path).read()
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return ast.literal_eval(node.value), src
    sys.exit(f"{name} not found in {path}")


JUDGE, _ = constant(RUBRIC_SCRIPT, "JUDGE")
EQ_SYS, EO_SOURCE = constant(EO_SCRIPT, "EQ_SYS")
if EQ_USER_SOURCE not in EO_SOURCE:
    sys.exit(f"The equivalence message in {EO_SCRIPT} is not the one this grader sends.")


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


ap = argparse.ArgumentParser(description="Grade a BEAM-1M memory-tools run.")
ap.add_argument("label")
ap.add_argument("--tasks", default=os.environ.get("BEAM_TASKS"))
ap.add_argument("--eo-split", choices=("trimmed", "raw"), default="trimmed")
ap.add_argument("--conc", type=int, default=8)
ap.add_argument("--runs-dir", default=os.path.join(HERE, "runs"))
a = ap.parse_args()
if not a.tasks:
    sys.exit("Name the task file: --tasks or BEAM_TASKS.")

if os.environ.get("OPENROUTER_API_KEY"):
    KEY, URL, MODEL = os.environ["OPENROUTER_API_KEY"], "https://openrouter.ai/api/v1/chat/completions", "openai/gpt-4.1-mini"
elif os.environ.get("OPENAI_API_KEY"):
    KEY, URL, MODEL = os.environ["OPENAI_API_KEY"], "https://api.openai.com/v1/chat/completions", "gpt-4.1-mini"
else:
    sys.exit("Set OPENROUTER_API_KEY or OPENAI_API_KEY for the judge.")
MODEL = os.environ.get("JUDGE_MODEL") or MODEL
URL = os.environ.get("JUDGE_URL") or URL

OUT = os.path.join(a.runs_dir, a.label)
QDIR = os.path.join(OUT, "questions")
rows = [json.load(open(os.path.join(QDIR, f))) for f in sorted(os.listdir(QDIR)) if f.endswith(".json")]
tasks = {t["question_id"]: t for t in json.load(open(a.tasks))}
missing = [r["question_id"] for r in rows if r["question_id"] not in tasks]
if missing:
    sys.exit(f"Not in the task file: {missing[:5]}")
unanswered = [r["question_id"] for r in rows
              if r.get("error") or r.get("blocked") or not (r.get("answer") or "").strip()]
if unanswered:
    print(f"warning: {len(unanswered)} questions have no answer (error, blocked or empty), e.g. {unanswered[:3]}; "
          "run.py again with the same label to answer them before grading", flush=True)

LOCK = threading.Lock()
RETURNED = collections.Counter()  # the model each judge response names


def chat(messages, max_tokens, timeout, wait_cap, tries=4, need_text=False):
    """The judge's reply, or None once every try failed. need_text: a reply with no content is tried again."""
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": max_tokens, "messages": messages}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(URL, data=body, method="POST",
                                         headers={"Authorization": "Bearer " + KEY, "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            with LOCK:
                RETURNED[str(d.get("model"))] += 1
            content = d["choices"][0]["message"]["content"]
            if need_text and not isinstance(content, str):
                raise ValueError("no content")
            return content or ""
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(min(wait_cap, 2 ** i))
    return None


# ── rubric ────────────────────────────────────────────────────────────────────

def score_item(job):
    """(score, reason); score None when the verdict cannot be read."""
    question, item, answer = job
    txt = chat([{"role": "user", "content": JUDGE.format(question=question, rubric_item=item, llm_response=answer)}],
               300, 120, 20)
    txt = (txt or "").strip()
    if not txt:
        return None, None
    m = re.search(r'"score"\s*:\s*([0-9.]+)', txt)
    if not m:
        return None, txt[:400]
    v = float(m.group(1))
    rm = re.search(r'"reason"\s*:\s*"(.*?)"\s*[,}]', txt, re.S)
    return (v if v in (0.0, 0.5, 1.0) else None), (rm.group(1)[:400] if rm else None)


jobs, index = [], []
for i, r in enumerate(rows):
    for item in tasks[r["question_id"]].get("rubric") or []:
        jobs.append((r["question"], item, r.get("answer") or ""))
        index.append(i)
print(f"{len(rows)} questions, {len(jobs)} rubric items, judge {MODEL}", flush=True)
t0 = time.time()
with ThreadPoolExecutor(max_workers=a.conc) as ex:
    verdicts = list(ex.map(score_item, jobs))
print(f"rubric judged in {(time.time() - t0) / 60:.1f} min", flush=True)

per, why = collections.defaultdict(list), collections.defaultdict(list)
for i, (s, reason), job in zip(index, verdicts, jobs):
    per[i].append(s)
    why[i].append({"rubric": job[1], "score": s, "reason": reason})
beam_rows = []
for i, r in enumerate(rows):
    got = [s for s in per[i] if s is not None]
    beam_rows.append({"question_id": r["question_id"], "user_id": r.get("user_id"), "type": r.get("type"),
                      "beam_score": (sum(got) / len(got)) if got else None,
                      "rubric_items": len(per[i]), "graded_items": len(got), "items": why[i]})
with open(os.path.join(OUT, "beam.json"), "w") as f:
    json.dump({"judge": MODEL, "judge_prompt": "scripts/beam_grade_gpt.py JUDGE", "rows": beam_rows}, f,
              ensure_ascii=False, indent=1)


# ── event ordering ────────────────────────────────────────────────────────────

EQ_FAILED = collections.Counter()


def equivalent(qid, ref, line):
    out = chat([{"role": "system", "content": EQ_SYS}, {"role": "user", "content": EQ_USER % (ref, line)}], 8, 90, 15,
               need_text=True)
    if out is None:
        with LOCK:
            EQ_FAILED[qid] += 1
        out = ""
    return "yes" in out.lower()


def align(qid, reference, system):
    used, system_out = set(), []
    for s in system:
        hit = None
        for i, r in enumerate(reference):
            if i in used:
                continue
            if equivalent(qid, r, s):
                hit = i
                break
        if hit is not None:
            system_out.append(reference[hit])
            used.add(hit)
        else:
            system_out.append(s)
    return reference, system_out


def kendall_tau_b(x, y):
    """Kendall's tau-b, ties included; None when it is undefined (fewer than two items, or one side all tied)."""
    n = len(x)
    if n < 2:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            s = (x[i] > x[j]) - (x[i] < x[j])
            t = (y[i] > y[j]) - (y[i] < y[j])
            if s * t > 0:
                conc += 1
            elif s * t < 0:
                disc += 1
    pairs = n * (n - 1) / 2

    def tied(v):
        return sum(k * (k - 1) / 2 for k in collections.Counter(v).values())

    d = math.sqrt((pairs - tied(x)) * (pairs - tied(y)))
    return (conc - disc) / d if d else None


def tau_norm(qid, reference, system):
    ref_c, sys_c = align(qid, reference, system)
    union = list(dict.fromkeys(ref_c + sys_c))
    tie = len(union) + 1

    def rank(seq):
        r = {x: i + 1 for i, x in enumerate(seq)}
        return [r.get(u, tie) for u in union]

    t = kendall_tau_b(rank(ref_c), rank(sys_c))
    return (t + 1) / 2 if t is not None else 0.0


def lines_of(answer):
    s = str(answer or "")
    if a.eo_split == "raw":
        return s.split("\n") if s.strip() else []
    return [x.strip() for x in s.split("\n") if x.strip()]


def eo_one(r):
    ref = tasks[r["question_id"]].get("rubric") or []
    sysl = lines_of(r.get("answer"))
    if not ref or not sysl:
        return r["question_id"], None
    return r["question_id"], tau_norm(r["question_id"], ref, sysl)


eo_rows = [r for r in rows if r.get("type") == "event_ordering"]
t0 = time.time()
with ThreadPoolExecutor(max_workers=a.conc) as ex:
    eo = dict(ex.map(eo_one, eo_rows))
print(f"event ordering: {len(eo_rows)} questions in {(time.time() - t0) / 60:.1f} min", flush=True)
with open(os.path.join(OUT, "eo.json"), "w") as f:
    json.dump(eo, f, indent=1)


# ── scores ────────────────────────────────────────────────────────────────────

official, rubric_all = {}, {}
by, by_rubric = collections.defaultdict(list), collections.defaultdict(list)
for r in beam_rows:
    if r["beam_score"] is None:
        continue
    rubric_all[r["question_id"]] = r["beam_score"]
    by_rubric[r["type"]].append(r["beam_score"])
    v = eo.get(r["question_id"]) if r["type"] == "event_ordering" else r["beam_score"]
    if v is None:
        continue
    official[r["question_id"]] = v
    by[r["type"]].append(v)


def pct(xs):
    return round(100 * st.fmean(xs), 2) if xs else None


score = {"score": pct(list(official.values())), "questions": len(official),
         "score_rubric": pct(list(rubric_all.values())), "questions_rubric": len(rubric_all),
         "by_type": {k: round(100 * st.fmean(v), 1) for k, v in by.items()},
         "by_type_rubric": {k: round(100 * st.fmean(v), 1) for k, v in by_rubric.items()},
         "per_question": official}
with open(os.path.join(OUT, "score.json"), "w") as f:
    json.dump(score, f, indent=1)

unread = sum(r["rubric_items"] - r["graded_items"] for r in beam_rows)
manifest_path = os.path.join(OUT, "manifest.json")
manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {"label": a.label}
manifest["grading"] = {
    "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "judge_model": MODEL, "judge_endpoint": "openrouter" if "openrouter.ai" in URL else
    "openai" if "api.openai.com" in URL else "custom",
    "judge_models_returned": dict(RETURNED), "temperature": 0,
    "judge_prompt": {"source": "scripts/beam_grade_gpt.py JUDGE", "sha256": sha256_text(JUDGE), "max_tokens": 300},
    "equivalence_prompt": {"source": "scripts/beam_eo.py EQ_SYS", "sha256": sha256_text(EQ_SYS), "max_tokens": 8},
    "eo_split": a.eo_split,
    "eo_split_note": "trimmed: lines trimmed, blank lines dropped (the published runs); "
                     "raw: split on newlines only (BEAM's compute_metrics.py)",
    "half_credit": 0.5,
    "rubric_items": len(jobs), "rubric_items_unread": unread,
    "event_ordering_scored": sum(1 for v in eo.values() if v is not None), "event_ordering_questions": len(eo_rows),
    "equivalence_failed": dict(EQ_FAILED),
    "unanswered": unanswered,
    "score": score["score"], "score_rubric": score["score_rubric"],
}
tmp = manifest_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)
os.replace(tmp, manifest_path)

print(f"official {score['score']} ({score['questions']} questions), rubric {score['score_rubric']}")
for k, v in sorted(by.items(), key=lambda kv: -st.fmean(kv[1])):
    print("  %-26s %.1f" % (k, 100 * st.fmean(v)))
if unread:
    print(f"{unread} rubric verdicts could not be read and are left out of their question's mean")
if EQ_FAILED:
    print(f"warning: {sum(EQ_FAILED.values())} equivalence calls failed in {len(EQ_FAILED)} questions "
          f"(counted as no): {sorted(EQ_FAILED)[:5]}; grade again")
