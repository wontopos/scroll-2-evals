# BEAM-100K: Claude memory vs Wos (Haiku 5.5 answers, Sonnet 5.5 grades)

As of 14 October 2026: Claude memory was measured with the platform defaults of that day (agent toolset `agent_toolset_20260401`, model `claude-haiku-5-5`, no memory instructions; Dreams was not available on the account). The feature may improve after this date.

Conversations: 20 (c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14, c15, c16, c17, c18, c19, c20) · questions scored on both arms: 400

| type | n | claude | wos |
|---|---|---|---|
| **all** | 400 | **39.2** | **54.8** |
| abstention | 40 | 90.0 | 58.8 |
| contradiction_resolution | 40 | 15.0 | 59.7 |
| event_ordering | 40 | 30.9 | 23.9 |
| information_extraction | 40 | 21.0 | 77.5 |
| instruction_following | 40 | 69.4 | 64.4 |
| knowledge_update | 40 | 20.6 | 51.9 |
| multi_session_reasoning | 40 | 34.1 | 58.4 |
| preference_following | 40 | 66.5 | 78.8 |
| summarization | 40 | 19.6 | 21.1 |
| temporal_reasoning | 40 | 25.0 | 53.8 |

## Cost (list price)

| part | USD |
|---|---|
| claude living | 19.05 |
| claude probe | 3.84 |
| judge | 8.75 |
| wos reader | 0.34 |

## Notes

- Claude probe sessions that opened memory: 99%
- Claude store per conversation: 4.1 files, 18,304 bytes
- Recall put in front of the reader: 10,608 characters per question