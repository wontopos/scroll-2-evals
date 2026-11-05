#!/usr/bin/env python3
"""Grade the 70 event_ordering questions again, the way the paper scores them.

BEAM's `report_results.py` sums this one type differently:

    if key == "event_ordering":  score += object['tau_norm']
    else:                        score += object['llm_judge_score']

A rubric mean cannot see order, so only this type comes out differently. At 70 of 700 questions it moves the total
by one to two points.

Steps (after compute_metrics.py)
  1. Cut the answer into lines. The authors' code replaces the extracted facts with `llm_response.split("\\n")`;
     here each line is also trimmed and blank lines are dropped, as the published runs were graded.
  2. Match each line to a rubric item with an LLM equivalence verdict (YES/NO). A rubric item matched once is not
     used again.
  3. Kendall tau-b over the matched order, then tau_norm = (tau + 1) / 2.
  The value summed is `tau_norm`, not `final_score` (= tau_norm x f1), because that is what the authors' code sums.
  Choosing the more plausible one would make the score impossible to set beside anyone else's.

With tau_b at 0, tau_norm is 0.5: an answer in no particular order still gets half. Read this type's score with
that floor in mind.

usage: beam_eo.py <reader results.json> <task file.json> <out.json>
env:   OPENROUTER_API_KEY (openai/gpt-4.1-mini through OpenRouter) or OPENAI_API_KEY (gpt-4.1-mini directly)
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

from scipy.stats import kendalltau

# The same judge through OpenRouter or directly from OpenAI. The model, the prompt and the temperature are the same
# either way, so the score is too; only who sends the bill changes.
OPENAI_DIRECT = not os.environ.get("OPENROUTER_API_KEY") and bool(os.environ.get("OPENAI_API_KEY"))
if OPENAI_DIRECT:
    KEY = os.environ["OPENAI_API_KEY"]
    API = "https://api.openai.com/v1/chat/completions"
else:
    KEY = os.environ.get("OPENROUTER_API_KEY") or sys.exit("Set OPENROUTER_API_KEY or OPENAI_API_KEY for the judge.")
    API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("JUDGE_MODEL",
                       "gpt-4.1-mini" if OPENAI_DIRECT else "openai/gpt-4.1-mini")
CONC = int(os.environ.get("CONC", "8"))

RUN, TASKS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]

EQ_SYS = ("You are a binary classifier.\n"
          "If the TWO snippets describe the SAME event/fact, reply **YES**\n"
          "Otherwise reply **NO**. No extra words.\n"
          "DO NOT provide any exaplanation.")


def ask(messages, tries=4):
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": 8,
                       "messages": messages}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Authorization": "Bearer " + KEY,
                         "content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"].lower()
        except Exception:
            if i == tries - 1:
                return ""
            time.sleep(min(15, 2 ** i))
    return ""


def equivalent(ref, sysl):
    out = ask([{"role": "system", "content": EQ_SYS},
               {"role": "user", "content": "First snippet: %s \n\n Second snippet: %s" % (ref, sysl)}])
    return "yes" in out


def align(reference, system):
    used, system_out = set(), []
    for s in system:
        hit = None
        for i, r in enumerate(reference):
            if i in used:
                continue
            if equivalent(r, s):
                hit = i
                break
        if hit is not None:
            system_out.append(reference[hit])
            used.add(hit)
        else:
            system_out.append(s)
    return reference, system_out


def tau_norm(reference, system):
    ref_c, sys_c = align(reference, system)
    union = list(dict.fromkeys(ref_c + sys_c))
    tie = len(union) + 1

    def rank(seq):
        r = {x: i + 1 for i, x in enumerate(seq)}
        return [r.get(u, tie) for u in union]

    t, _ = kendalltau(rank(ref_c), rank(sys_c), variant="b", method="auto")
    return (t + 1) / 2 if t is not None else 0.0


rubrics = {t["question_id"]: t["rubric"] for t in json.load(open(TASKS))}
rows = [r for r in json.load(open(RUN))["rows"] if r.get("type") == "event_ordering"]
print("event_ordering: %d questions, equivalence judge %s" % (len(rows), MODEL), flush=True)


def one(r):
    ref = rubrics.get(r["question_id"], [])
    sysl = [x.strip() for x in str(r.get("answer") or "").split("\n") if x.strip()]
    if not ref or not sysl:
        return r["question_id"], None
    return r["question_id"], tau_norm(ref, sysl)


t0 = time.time()
with ThreadPoolExecutor(max_workers=CONC) as ex:
    res = dict(ex.map(one, rows))
print("judged in %.1f min" % ((time.time() - t0) / 60), flush=True)

json.dump(res, open(OUT, "w"), indent=1)
import statistics as st
ok = [v for v in res.values() if v is not None]
print("event_ordering: mean tau_norm %.1f%% (%d questions)" % (st.fmean(ok) * 100, len(ok)))
