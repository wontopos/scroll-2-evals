#!/usr/bin/env python3
"""BEAM's rubric grading, with the judge and temperature of the authors' code.

A different judge moves the score (the paper itself allows about one point for judge disagreement), so this uses
**gpt-4.1-mini at temperature 0**, the `gpt_llm_obj` of the authors' `src/llm.py`, not GPT-4o. Choosing a better
judge would make the score impossible to set beside anyone else's.

The judge prompt below is the one every published run was graded with. Editing its wording so that scores come out
higher would be grading ourselves.
  each rubric item scores 1.0 / 0.5 / 0.0, and a question's score is their mean (compute_metrics.py)

An item whose verdict cannot be read is left out of the denominator, not counted as 0. Counting a judge failure as a
wrong answer turns a grader outage into an engine result.

usage: beam_grade_gpt.py <reader results.json> <task file.json> <out.json>
env:   OPENROUTER_API_KEY (openai/gpt-4.1-mini through OpenRouter) or OPENAI_API_KEY (gpt-4.1-mini directly)
"""
import json, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

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

JUDGE = """
You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- QUESTION (what the user asked): {question}
- RUBRIC CRITERION (what to check): {rubric_item}
- RESPONSE TO EVALUATE: {llm_response}

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)
A compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms. Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure.
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
- **0.5 (Partial Compliance)**: Partially complies; element present but minor inaccuracies/incomplete execution.
- **0.0 (No Compliance)**: Required element missing or incorrect, or response non-responsive.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[short explanation]"
}}

NOTE: ONLY output the json object, without any explanation before or after that
"""

RUN, TASKS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]


def ask(prompt, tries=4):
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": 300,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"Authorization": "Bearer " + KEY,
                         "content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            return (d["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(min(20, 2 ** i))
    return None


def score_one(args):
    """The score and the judge's reason, together.

    The reason is kept. With the score alone, "why was this a 0.5" has to be asked again, which pays for the same
    verdict twice; these sentences are also the evidence a write-up quotes."""
    q, item, ans = args
    txt = ask(JUDGE.format(question=q, rubric_item=item, llm_response=ans))
    if not txt:
        return (None, None)
    m = re.search(r'"score"\s*:\s*([0-9.]+)', txt)
    if not m:
        return (None, txt[:400])
    v = float(m.group(1))
    reason = None
    rm = re.search(r'"reason"\s*:\s*"(.*?)"\s*[,}]', txt, re.S)
    if rm:
        reason = rm.group(1)[:400]
    return ((v if v in (0.0, 0.5, 1.0) else None), reason)


rubrics = {t["question_id"]: t["rubric"] for t in json.load(open(TASKS))}
rows = json.load(open(RUN))["rows"]

jobs, index = [], []
for i, r in enumerate(rows):
    for item in rubrics.get(r["question_id"], []):
        jobs.append((r["question"], item, r.get("answer") or ""))
        index.append(i)

print("%d questions, %d rubric items, judge %s" % (len(rows), len(jobs), MODEL), flush=True)
t0 = time.time()
with ThreadPoolExecutor(max_workers=CONC) as ex:
    scores = list(ex.map(score_one, jobs))
print("judged in %.1f min" % ((time.time() - t0) / 60), flush=True)

per, why = {}, {}
for i, (s, reason), item in zip(index, scores, [j[1] for j in jobs]):
    per.setdefault(i, []).append(s)
    why.setdefault(i, []).append({"rubric": item, "score": s, "reason": reason})

out = []
for i, r in enumerate(rows):
    got = [s for s in per.get(i, []) if s is not None]
    out.append({**{k: r.get(k) for k in ("question_id", "user_id", "type")},
                "beam_score": (sum(got) / len(got)) if got else None,
                "rubric_items": len(per.get(i, [])), "graded_items": len(got),
                "context_chars": r.get("context_chars"), "wos_ms": r.get("wos_ms"),
                "items": why.get(i, [])})

json.dump({"rows": out, "judge": MODEL,
           "judge_source": "BEAM unified_llm_judge_base_prompt",
           "note": "event_ordering: rubric mean only, Kendall's tau not computed"},
          open(OUT, "w"), ensure_ascii=False, indent=1)

import collections, statistics as st
ok = [o for o in out if o["beam_score"] is not None]
print("\n%d questions, rubric score %.1f%%" % (len(ok), st.fmean(o["beam_score"] for o in ok) * 100))
by = collections.defaultdict(list)
for o in ok:
    by[o["type"]].append(o["beam_score"])
print("\n%-28s%8s%9s" % ("type", "n", "score"))
for t in sorted(by, key=lambda x: -st.fmean(by[x])):
    print("%-28s%8d%8.1f%%" % (t, len(by[t]), st.fmean(by[t]) * 100))
skipped = sum(o["rubric_items"] - o["graded_items"] for o in out)
if skipped:
    print("\n%d items with no readable verdict, left out of the denominator" % skipped)
