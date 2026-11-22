# Matching eval results

Dataset: 30 labeled pairs (2 personas). Embedder: `hash-embedder`. Stage 2: `off (basic scores)`.

| Persona | P@5 | P@10 | Pairwise acc. | Spearman ρ | Hard-filtered |
|---|---|---|---|---|---|
| fresher_backend_ai | 0.80 | 0.40 | 1.00 | 0.84 | 5 |
| mid_data_analyst | 0.60 | 0.30 | 1.00 | 0.87 | 7 |
| **mean** | **0.70** | **0.35** | **1.00** | **0.85** | |

P@10 is capped by how many jobs are labeled *good* per persona (4–5 of 15).

## fresher_backend_ai

| Rank | Job | Score | Label | Note |
|---|---|---|---|---|
| 1 | j03 | 51 | good |  |
| 2 | j10 | 48 | good |  |
| 3 | j01 | 47 | good |  |
| 4 | j02 | 43 | good |  |
| 5 | j13 | 39 | ok |  |
| 6 | j05 | 36 | bad |  |
| 7 | j15 | 31 | ok |  |
| 8 | j14 | 28 | bad |  |
| 9 | j08 | 24 | bad |  |
| 10 | j09 | 22 | ok |  |
| 11 | j04 | 0 | bad | filtered: seniority |
| 12 | j06 | 0 | bad | filtered: seniority |
| 13 | j07 | 0 | bad | filtered: seniority |
| 14 | j11 | 0 | bad | filtered: location |
| 15 | j12 | 0 | bad | filtered: seniority |

## mid_data_analyst

| Rank | Job | Score | Label | Note |
|---|---|---|---|---|
| 1 | j12 | 56 | good |  |
| 2 | j07 | 54 | good |  |
| 3 | j14 | 53 | ok |  |
| 4 | j06 | 52 | good |  |
| 5 | j08 | 43 | ok |  |
| 6 | j13 | 41 | bad |  |
| 7 | j02 | 23 | bad |  |
| 8 | j10 | 17 | bad |  |
| 9 | j01 | 0 | bad | filtered: job_type |
| 10 | j03 | 0 | bad | filtered: location |
| 11 | j04 | 0 | bad | filtered: seniority |
| 12 | j05 | 0 | bad | filtered: location |
| 13 | j09 | 0 | bad | filtered: job_type |
| 14 | j11 | 0 | bad | filtered: location |
| 15 | j15 | 0 | bad | filtered: job_type |
