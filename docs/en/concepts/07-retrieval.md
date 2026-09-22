# Retrieval Mechanism

OpenViking retrieves context through vector search and directory traversal. `search()` can analyze query intent first; a configured reranker can refine candidate ordering.

## Overview

```
Query → Intent Analysis → Hierarchical Retrieval → Rerank → Results
              ↓                    ↓                  ↓
         TypedQuery          Directory Recursion   Refined Scoring
```

## find() vs search()

| Feature | find() | search() |
|---------|--------|----------|
| Session context | Not used | Optional; used when `session_id` is supplied |
| Intent analysis | Not used | Uses an LLM when session content exists and intent analysis is enabled |
| Query count | Single query | zero or more TypedQueries |
| Latency | Low | Higher |
| Use case | Simple queries | Complex tasks |

### Usage Examples

```python
# find(): Simple query
results = await client.find(
    query="OAuth authentication",
    target_uri="viking://resources/",
)

# search(): Complex task (needs session context)
session_info = await client.create_session()
await client.add_message(
    session_id=session_info["session_id"], role="user",
    content="We are designing the OAuth login flow for this project.",
)
results = await client.search(
    query="Help me create an RFC document",
    session_id=session_info["session_id"],
)
```

## Intent Analysis

When `retrieval.enable_intent=true` and the session contains a summary or messages, IntentAnalyzer uses an LLM to analyze query intent and generate zero or more TypedQueries. The model used for this stage is separately configurable via the [`query_planner`](../guides/01-configuration.md#query-planner) config, falling back to `vlm` when unset.

### Input

- Session compression summary
- Last 5 messages
- Current query

### Output

```python
@dataclass
class TypedQuery:
    query: str              # Rewritten query
    context_type: ContextType  # MEMORY/RESOURCE/SKILL
    intent: str             # Query purpose
    priority: int           # 1-5 priority
```

### Query Styles

| Type | Style | Example |
|------|-------|---------|
| **skill** | Verb-first | "Create RFC document", "Extract PDF tables" |
| **resource** | Noun phrase | "RFC document template", "API usage guide" |
| **memory** | "User's XX" | "User's code style preferences" |

### Special Cases

- **0 queries**: Chitchat, greetings that don't need retrieval
- **Multiple queries**: Complex tasks may need skill + resource + memory

## Hierarchical Retrieval

HierarchicalRetriever uses priority queue to recursively search directory structure.

### Flow

```
Step 1: Determine root directories by context_type
        ↓
Step 2: Global vector search to locate starting directories
        ↓
Step 3: Merge starting points + Rerank scoring
        ↓
Step 4: Recursive search (priority queue)
        ↓
Step 5: Convert to MatchedContext
```

### Root Directory Mapping

| context_type | Root Directories |
|--------------|------------------|
| MEMORY | `viking://~/memories` |
| RESOURCE | `viking://resources` |
| SKILL | `viking://~/skills` and `viking://agent/skills` |

### Recursive Search Algorithm

```python
while dir_queue:
    current_uri, parent_score = heapq.heappop(dir_queue)

    # Search children
    results = await search(parent_uri=current_uri)

    for r in results:
        # Score propagation
        final_score = score_propagation_alpha * embedding_score + (1 - score_propagation_alpha) * parent_score

        if final_score > threshold:
            collected.append(r)

            if not r.is_leaf:  # Directory continues recursion
                heapq.heappush(dir_queue, (r.uri, final_score))

    # Convergence detection
    if topk_unchanged_for_3_rounds:
        break
```

### Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `retrieval.score_propagation_alpha` | 1.0 | Child-score weight in the propagation blend; `1.0` uses only the child's own score and ignores the parent score |
| `MAX_CONVERGENCE_ROUNDS` | 3 | Convergence detection rounds |
| `GLOBAL_SEARCH_TOPK` | 10 | Global search candidates |

## Rerank Strategy

Rerank refines candidate results in THINKING mode.

### Trigger Conditions

- A reranking model and its credentials are configured
- Using THINKING mode (default for search())
- If rerank returns an invalid result or the API call fails, retrieval falls back to vector scores

### Scoring Method

```python
if rerank_client and mode == THINKING:
    scores = rerank_client.rerank_batch(query, documents)
else:
    scores = [r["_score"] for r in results]  # Vector scores
```

### Usage Points

1. **Starting point evaluation**: Evaluate global search candidate directories
2. **Recursive search**: Evaluate children at each level

### Backend Support

| Backend | Model |
|---------|-------|
| Volcengine | doubao-seed-rerank |

## Retrieval Results

### MatchedContext

```python
@dataclass
class MatchedContext:
    uri: str                # Resource URI
    context_type: ContextType
    is_leaf: bool           # Whether file
    abstract: str           # L0 abstract
    score: float            # Final score
```

### FindResult

```python
@dataclass
class FindResult:
    memories: List[MatchedContext]
    resources: List[MatchedContext]
    skills: List[MatchedContext]
    query_plan: Optional[QueryPlan]      # Present for search()
    query_results: Optional[List[QueryResult]]
    total: int
```

## Related Documents

- [Architecture Overview](./01-architecture.md) - System architecture
- [Storage Architecture](./05-storage.md) - Vector index
- [Context Layers](./03-context-layers.md) - L0/L1/L2 model
- [Context Types](./02-context-types.md) - Three context types
