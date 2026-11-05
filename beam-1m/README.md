# BEAM-1M

## Protocol

| | |
|---|---|
| benchmark | BEAM-1M: 35 conversations, 700 questions |
| engine | Scroll 2: one build for the five single-call runs; each memory-tools run on the engine of its date |
| memories to the reader | single call: the first 20 and three further pages (70.2 on average) |
| reader | Claude Opus 5, maximum effort, through `claude -p`; memory text only, without dates or speakers |
| judge | gpt-4.1-mini, temperature 0, the same judge prompt as Tablet 2's published runs, once per rubric item |
| event ordering | Kendall tau-b normalised, (tau+1)/2, as in BEAM's `report_results.py`; also given by rubric mean |

## Score

| run | date | event ordering by tau | by rubric |
|---|---|---|---|
| `beam700_w80` | 28 Sep 2026 | 71.55 | 75.55 |
| `beam700_w80b` | 30 Sep 2026 | 72.71 | 76.77 |
| `beam700_w80c` | 6 Oct 2026 | 72.68 | 76.97 |
| `beam700_w80d` | 6 Oct 2026 | 72.02 | 76.10 |
| `beam700_w80e` | 6 Oct 2026 | 71.55 | 75.59 |
| **mean of five** | | **72.10** (sd 0.51) | **76.20** (sd 0.58) |

Against Tablet 2 (five runs, same reader and judge, [`wontopos/beam1m-tablet-2`](https://github.com/wontopos/beam1m-tablet-2)), per question, 2,000 paired bootstrap resamples: tau +4.6 [3.4, 5.9], rubric +4.9 [3.6, 6.2].

### With memory tools (four runs)

| run | date | event ordering by tau | by rubric |
|---|---|---|---|
| `beam700_wtp` | 30 Sep 2026 | 72.98 | 77.36 |
| `beam700_rep930` | 18 Oct 2026 | 72.77 | 76.90 |
| `beam700_sdkoff1019` | 19 Oct 2026 | 73.32 | 77.93 |
| `beam700_sleep_off` | 20 to 21 Oct 2026 | 73.67 | 78.06 |
| **mean of four** | | **73.18** (sd 0.34) | **77.56** (sd 0.46) |

The reader gets the first 20 memories and may ask for more with the memory tools; memory text only, no dates or speakers. Against the mean of the five single-call runs: tau +1.1 [−0.2, 2.3], rubric +1.4 [0.0, 2.6]. Against Tablet 2: tau +5.7 [4.1, 7.2], rubric +6.2 [4.7, 7.7]. Tool use per question is in `runs/beam700_wtp.tools.json` and `runs/beam700_rep930.tools.json`, and in the rows of `runs/beam700_sdkoff1019.json` and `runs/beam700_sleep_off.json`. The four runs used an earlier wording of the `memory_search` description; SDK 2.2.49 words it more briefly, and the harness's self-test checks the release's texts.

### Sleep 1.0.0 (one run each)

Memory tools as above, on 20 and 21 October 2026. Sleep off is `beam700_sleep_off`, the fourth memory-tools run. Sleep on: Sleep 1.0.0 written by Claude Sonnet 5.5 at high effort, once for each conversation after its last session; when answering, the memory tools can open the files it wrote.

| run | event ordering by tau | by rubric |
|---|---|---|
| `beam700_sleep_off` | 73.67 | 78.06 |
| `beam700_sleep_on` | 73.55 | 78.57 |
| difference | −0.1 [−1.9, 1.7] | +0.5 [−1.3, 2.4] |

| ability | sleep on − off |
|---|---|
| instruction following | +7.1 [1.3, 14.5] |
| summarization | +6.3 [1.4, 11.1] |
| event ordering (rubric) | +5.5 [1.5, 9.5] |
| knowledge update | +0.4 [−6.1, 7.5] |
| preference following | +0.4 [−4.3, 5.1] |
| contradiction resolution | −0.2 [−5.4, 4.8] |
| temporal reasoning | −0.2 [−6.2, 5.8] |
| multi-session reasoning | −1.7 [−7.4, 3.7] |
| information extraction | −4.7 [−10.8, 0.2] |
| abstention | −7.9 [−16.4, 0.0] |

Per question, 2,000 paired bootstrap resamples. One sleep per conversation, at Claude Sonnet 5.5's list prices without prompt caching: $4.29 per million tokens of memory read on average ($3.79 to $4.59 across the 35 conversations), about 289 seconds. Per conversation in `runs/beam700_sleep_on.sleep.json`.

## Coverage

A judge (gpt-4.1-mini, temperature 0) reads the question, the reference answer, the rubric items and the memories the reader received, never the reader's answer, and scores each rubric item 1, 0.5 or 0 for whether the memories hold what that item needs. For instruction following and preference following the item is whether the memories hold the user's own instruction or preference. Abstention is left out, which leaves 630 questions. Scroll 2: the memories of `beam700_w80b` and `beam700_wtp`.

| memories | Scroll 2 | Tablet 2 |
|---|---|---|
| first 20 | 57.7 | 51.8 |
| first 40 | 64.5 | 59.8 |
| all | 69.0 | 65.4 |

The 625 questions the Tablet 2 replay covers. Scroll 2 on all 630: 69.1, with memory tools 68.4.

| ability | Coverage | score |
|---|---|---|
| preference following | 40.0 | 91.1 |
| information extraction | 83.8 | 84.6 |
| contradiction resolution | 81.4 | 83.2 |
| knowledge update | 85.4 | 83.6 |
| instruction following | 71.4 | 78.7 |
| multi-session reasoning | 79.9 | 73.3 |
| temporal reasoning | 67.5 | 53.0 |
| summarization | 59.9 | 65.5 |
| event ordering (rubric) | 52.2 | 64.7 |

Scroll 2, `beam700_w80b`, 630 questions.

## Store size

Each BEAM store cut at nine points, from 0.2% of its turns to all of them, by storage time; at each cut its 20 questions asked the single-call way. Engine: Scroll 2 as of 2 October 2026. 6,300 questions, no errors. In four stores (conversations 5, 26, 33 and 34) the smallest cut already showed 71 to 92% of the store, so the cut did not shrink them; the table uses the other 31 (5,580 questions).

| share of turns | conversation tokens | memories in store | memories back | tokens back (median) | middle half |
|---|---|---|---|---|---|
| 0.2% | 1,058 | 77 | 67 | 2,361 | 2,173 to 2,646 |
| 0.5% | 3,002 | 218 | 71 | 2,732 | 2,415 to 3,127 |
| 1% | 6,879 | 496 | 71 | 2,856 | 2,468 to 3,349 |
| 2% | 15,980 | 1,104 | 71 | 2,997 | 2,413 to 3,535 |
| 5% | 43,910 | 2,908 | 70 | 3,122 | 2,443 to 3,775 |
| 10% | 89,910 | 5,906 | 71 | 3,212 | 2,689 to 3,858 |
| 20% | 179,787 | 11,807 | 71 | 3,278 | 2,724 to 3,938 |
| 50% | 481,225 | 30,461 | 71 | 3,358 | 2,850 to 4,007 |
| 100% | 973,200 | 61,215 | 71 | 3,351 | 2,801 to 3,995 |

Medians over questions. Tokens counted with o200k_base. Engine time, one request at a time on conversations 2, 3 and 4: opening 146 ms at 79 memories and 185 ms at 49,372; each further page 145 and 162 ms.

## Files

```
runs/                 per question: answer, memories handed over, context size, engine time, error
runs/beam700_wtp.tools.json   memory tool calls per question
runs/beam700_sleep_on.sleep.json   per conversation: memory read by the sleep, its model's tokens, list-price cost, time, size of the files it wrote
grades/*.beam.json    per question: score, rubric items graded, the judge's score and reason per item
grades/*.eo.json      event ordering, normalised Kendall tau per question
coverage/             per question: Coverage per rubric item with the judge's reason
store-size/           per question and cut: memories and tokens back, time per request
scripts/              BEAM grading and scoring
harness/              memory-tools runs through the public SDK, with a self-test
```

Rubric items are joined to BEAM's rubric by position; the rubric text is not included.
