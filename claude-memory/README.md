# Claude memory and Scroll 2 (BEAM-100K)

**As of 14 October 2026.** Claude memory was measured with the platform defaults of that day. It may improve after this date.

## Protocol

| | |
|---|---|
| benchmark | BEAM-100K: 20 conversations, 2,866 user turns, 400 questions |
| reader | Claude Haiku 5.5 in both arms |
| judge | Claude Sonnet 5.5, BEAM's rubric prompt, once per rubric item |
| runs | one |
| engine | Scroll 2 as of 2 October 2026 |
| Claude memory | Claude Managed Agents with a memory store attached read-write, agent toolset `agent_toolset_20260401`, web tools off, no memory instructions. The user's turns are sent session by session with the session date; the agent answers them and keeps its own memory. Each question then goes to a new session with the store read-only. Dreams was not available on the account. |
| Scroll 2 | The conversation as it was (user and assistant, dated) stored in Scroll 2; the recall for each question put in front of the reader. |

## Score

| type (40 each) | Claude memory | Scroll 2 |
|---|---|---|
| **all 400** | **39.2** | **54.8** |
| abstention | 90.0 | 58.8 |
| contradiction resolution | 15.0 | 59.7 |
| event ordering (rubric) | 30.9 | 23.9 |
| information extraction | 21.0 | 77.5 |
| instruction following | 69.4 | 64.4 |
| knowledge update | 20.6 | 51.9 |
| multi-session reasoning | 34.1 | 58.4 |
| preference following | 66.5 | 78.8 |
| summarization | 19.6 | 21.1 |
| temporal reasoning | 25.0 | 53.8 |

Per question, 2,000 paired bootstrap resamples: Scroll 2 − Claude memory +15.6 [11.0, 20.4].

## Where the evidence is

The speaker of each question's evidence turns, from BEAM's `source_chat_ids` and `chat_id` references (`evidence_roles.json`).

| questions | n | Claude memory | Scroll 2 | Scroll 2 − Claude memory |
|---|---|---|---|---|
| all | 400 | 39.2 | 54.8 | +15.6 [11.0, 20.4] |
| evidence in user turns only | 285 | 37.1 | 57.9 | +20.8 [15.0, 26.5] |
| evidence includes assistant turns | 67 | 20.2 | 44.1 | +23.8 [15.2, 32.3] |

Abstention (40) and five more have no evidence turns; three have references that do not resolve to a turn.

## Checks

- The Claude memory arm received the user turns of its own conversation and nothing else. When the same user message appears twice in a conversation, the second was not sent: 8 of 2,866 turns, in conversations 5, 11, 12, 18 and 20.
- One memory store per conversation. No question or reference answer text in any store.
- 400 question sessions, each new, none shared with the sessions that played the conversation, no memory writes during questions.
- The judge saw the question, one rubric item and the answer, not the arm. No unreadable verdicts.
- Of 483 writes the Claude agent made while playing the conversations, 31 went to the session's output folder rather than the memory store.

## Files

```
results/c<N>/answers.json          per question and arm: answer, memory tool calls (Claude), recall size (Scroll 2), usage
results/c<N>/grades.json           per question, arm and rubric item: score and the judge's reason
results/c<N>/summary.json          score by type and arm for the conversation
results/c<N>/claude-store.json     the files in Claude's memory store after the conversation
results/c<N>/claude-sessions.json  per played session: turns, memory tool calls, usage
evidence_roles.json                per question: evidence turn ids and their speakers
RESULTS.md                         score table and cost
```
