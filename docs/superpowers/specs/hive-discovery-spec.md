# Hive Discovery: Swarm Architecture for FORGE Discovery Phase

**Status:** Implemented
**Date:** 2026-02-26
**Scope:** Discovery (Agents 1-4) + Triage (Agents 5-6) only. Remediation phase (Agents 7-12) is unchanged.

---

## Implementation Notes

Hive Discovery is fully implemented and tested:

- **Core modules**: `forge/swarm/worker.py` (swarm workers), `forge/swarm/synthesizer.py` (Layer 2 synthesis), `forge/swarm/orchestrator.py` (wave orchestration)
- **Layer 0**: `forge/graph/builder.py` provides deterministic code graph construction (AST parsing, dependency extraction, community detection)
- **Feature flag**: `config.discovery_mode = "swarm" | "classic"` (default: `"classic"`)
- **Live E2E tests**: 3 tests in `tests/integration/test_hive_live_e2e.py`, gated behind `--run-live`

---

## Problem Statement

FORGE's Discovery phase relies on a single Minimax M2.5 call (Agent 1: Codebase Analyst) to build the entire `CodebaseMap`. This map is the foundation for everything downstream — Agents 2-4 read it, triage depends on it, the remediation plan is built from it. If Agent 1 produces a weak or incomplete map, the quality degrades across the entire pipeline.

Additionally, context flows sequentially with no cross-referencing. Agent 2 (Security) cannot see what Agent 3 (Quality) found. Agent 4 (Architecture) cannot validate against Agent 2's security findings. Each agent operates in isolation within its phase.

The hypothesis: **better architecture can make cheap models collectively outperform what any single cheap model can do alone** — without upgrading model tiers.

---

## Research Basis

### Evidence That Architecture > Model Size

| Pattern | Key Finding | Source |
|---------|-------------|--------|
| **Mixture-of-Agents (MoA)** | Open-source models collectively beat GPT-4o (65.1% vs 57.5% on AlpacaEval) when arranged in layers where each agent sees outputs from previous agents | ICLR 2025 Spotlight |
| **COPE Framework** | Llama-3B alone: 42.8% accuracy. With planning scaffolding: 53.0%. Matches GPT-4o at 50% cost on MATH-500 | Efficient LLM Collaboration via Planning |
| **DisCIPL (MIT)** | Small models steered by structured programs outperform GPT-4o and approach o1. 80% cost savings, 40% shorter reasoning | MIT CSAIL, Dec 2025 |
| **CodePrism** | Universal AST graph: 1,247 files/sec indexing, 0.12ms symbol lookup, 2.1ms cross-file dependencies. Language-agnostic | CodePrism project |
| **GraphRAG** | Knowledge graph as shared agent memory with community detection. Proven in production for multi-agent coordination | Microsoft Research / Neo4j deployments |

### Key Property: "Collaborativeness"

The MoA paper identified that LLMs generate better responses when presented with outputs from other models — even less capable ones. This property is called "collaborativeness" and it means cheap model outputs can prime and improve other cheap model outputs when structured correctly.

### Warnings From Production Systems

| Warning | Source | How We Address It |
|---------|--------|-------------------|
| Pure parallel decomposition without coordination degrades quality | Cognition (Devin) team | Two-wave execution with explicit sync point between waves |
| LLM swarms have latency overhead vs single-agent approaches | NVIDIA swarm research (2025) | Keep total invocations bounded; Wave 2 is optional |
| Smaller models struggle with deep multi-step reasoning | Enterprise SLM research | Reserve Sonnet 4.6 for the synthesis/aggregation step where deep reasoning matters most |

---

## Proposed Architecture: Three-Layer Hive

Replace the current serial Agent 1 → parallel Agents 2-4 → serial Agents 6, 5 flow with:

```
LAYER 0: Code Graph Builder (deterministic, no LLM)
  Build a code knowledge graph from AST + dependency analysis
  Segment codebase into analyzable clusters
    |
    v
LAYER 1: Swarm Analysis (many minimax-m2.5 workers, parallel)
  N workers analyze their assigned segments
  Each worker writes findings TO the shared graph
  Each worker reads OTHER workers' findings FROM the graph
    |
    v
LAYER 2: Synthesis (single sonnet-4.6 call)
  Reads the full enriched graph
  Cross-references, deduplicates, validates findings
  Produces final CodebaseMap + Findings + Triage + RemediationPlan
```

### Layer 0: Code Graph Builder (Deterministic)

No LLM involved. Pure static analysis. This replaces the structural indexing that Agent 1 currently does with an LLM call.

```
Input: Cloned repository
    |
    +-- Tree-sitter AST parsing (per-file)
    |     Nodes: Module, Class, Function, Import, Variable
    |     Edges: Calls, Imports, Inherits, References
    |
    +-- Dependency graph extraction
    |     package.json / requirements.txt / go.mod / Cargo.toml
    |     Internal module dependency DAG
    |
    +-- Community detection (Leiden algorithm or simple modularity)
    |     Cluster tightly-coupled files into "segments"
    |     Target: 3-8 segments per codebase (adjustable)
    |
    +-- Output: CodeGraph
          {
            "nodes": [...],
            "edges": [...],
            "segments": [
              {
                "id": "seg-auth",
                "files": ["auth.py", "middleware.py", "jwt.py"],
                "entry_points": ["login()", "verify_token()"],
                "external_deps": ["pyjwt", "bcrypt"],
                "internal_deps": ["seg-db", "seg-config"]
              }
            ],
            "stats": {
              "total_files": 142,
              "total_loc": 28000,
              "languages": {"python": 0.85, "javascript": 0.15}
            }
          }
```

**Why deterministic?** Agent 1's current job is 80% structural indexing (modules, deps, data flows, tech stack) that doesn't need an LLM. Moving this to static analysis removes the single-model bottleneck and produces a more reliable foundation.

**Implementation:** New module `forge/graph/builder.py` using Tree-sitter for AST parsing and a lightweight community detection algorithm for segmentation.

### Layer 1: Swarm Analysis (Parallel, Graph-Connected)

Each segment gets analyzed by 3 specialist workers running in parallel. All workers use **Minimax M2.5** — the same cheap model currently used for Agents 1 and 3.

```
Per segment (e.g., "seg-auth"):
  +-- Security Worker (minimax-m2.5)
  |     Reads: segment files + graph neighbors + graph edges
  |     Writes: security findings --> graph nodes
  |
  +-- Quality Worker (minimax-m2.5)
  |     Reads: segment files + graph neighbors + graph edges
  |     Writes: quality findings --> graph nodes
  |
  +-- Architecture Worker (minimax-m2.5)
        Reads: segment files + graph neighbors + cross-segment edges
        Writes: architecture observations --> graph nodes
```

**Two-wave execution** (following Cognition's coordination warning):

- **Wave 1:** All workers analyze their primary segment in parallel. No cross-segment context yet.
- **Sync point:** Wave 1 findings are written to the shared graph.
- **Wave 2:** Workers re-analyze their segment, this time with access to Wave 1 findings from neighboring segments via the graph. This is the MoA "collaborativeness" property — each worker produces better output when it can see what other workers found.

**Worker count scales with codebase size:**

| Repo Size | Segments | Workers (W1 + W2) | Total Invocations |
|-----------|----------|--------------------|--------------------|
| Small (<50 files) | 1 | 3 + 3 | 6 |
| Medium (50-200 files) | 4 | 12 + 12 | 24 |
| Large (200+ files) | 6-8 | 18-24 + 18-24 | 36-48 |

### Layer 2: Synthesis (Single Aggregator)

One **Sonnet 4.6** call that:

1. Reads the full enriched graph (all segment findings + their relationships)
2. Cross-references findings across segments (e.g., auth issue in seg-auth + no validation in seg-api = critical chain)
3. Deduplicates overlapping findings from different workers
4. Assigns confidence scores based on how many workers flagged similar issues
5. Assigns tiers (0-3) — merging current Agents 5 and 6 into this step
6. Produces the final `CodebaseMap` + `AuditFinding[]` + `RemediationPlan` in the existing schema

**Why Sonnet 4.6 here?** The DisCIPL and COPE research both show that the synthesis/aggregation step is where model quality matters most. Cheap models do the distributed work; a strong model does the judgment. This is a single call, so the cost is bounded (~$0.15).

### Architecture Diagram

```
                  +------------------------------------+
                  |    LAYER 0: Code Graph Builder      |
                  |    (deterministic, no LLM)          |
                  |                                    |
                  |  Tree-sitter -> AST -> Dependency   |
                  |  Graph -> Community Detection ->    |
                  |  Segments                           |
                  +----------------+-------------------+
                                   | CodeGraph + Segments
                  +----------------v-------------------+
                  |    SHARED GRAPH MEMORY (in-mem)     |
                  |                                    |
                  |  Nodes: files, functions, classes,   |
                  |         findings, observations       |
                  |  Edges: calls, imports, inherits,    |
                  |         affects, related_to          |
                  +--+--------+--------+--------------+
                     |        |        |
        +------------v--+ +--v--------+ +--v----------+
        |  Segment A     | | Segment B  | | Segment C    |
        |                | |            | |              |
        | +-Security-+   | | +-Sec--+  | | +-Sec--+    |
        | +-Quality--+   | | +-Qual-+  | | +-Qual-+    |
        | +-Arch-----+   | | +-Arch-+  | | +-Arch-+    |
        |                | |            | |              |
        |  WAVE 1: solo  | |  WAVE 1    | |  WAVE 1      |
        |  WAVE 2: +graph| |  WAVE 2    | |  WAVE 2      |
        +--------+-------+ +----+-------+ +----+--------+
                 |              |              |
                 |  findings written to graph  |
                 +--------------+--------------+
                                |
                  +-------------v------------------+
                  |    LAYER 2: Synthesis Agent      |
                  |    (sonnet-4.6)                  |
                  |                                  |
                  |  Reads full enriched graph        |
                  |  Cross-references across segments |
                  |  Deduplicates + validates          |
                  |  Assigns tiers (0-3)              |
                  |  Produces RemediationPlan          |
                  |                                  |
                  |  Output: CodebaseMap + Findings[] |
                  |          + RemediationPlan        |
                  +----------------+-------------------+
                                   |
                  +----------------v-------------------+
                  |  Existing Remediation Phase         |
                  |  (Agents 7-12, unchanged)           |
                  +------------------------------------+
```

---

## Tradeoffs vs Original FORGE Spec

| Aspect | Original Spec | Hive Discovery | Winner |
|--------|--------------|----------------|--------|
| **Agent 1 bottleneck** | Single minimax-m2.5 builds entire CodebaseMap — single point of failure | Deterministic Layer 0 replaces structural indexing; no LLM bottleneck | Hive |
| **Context sharing** | Sequential pipeline. Agents 2-4 see CodebaseMap only. No cross-agent visibility | Shared graph memory. Wave 2 workers see all Wave 1 findings from neighbors | Hive |
| **Cross-segment findings** | Not possible. Each agent sees the full codebase but analyzes independently | Built-in via graph neighbor queries. Workers explicitly see related findings | Hive |
| **Discovery models** | minimax-m2.5 (Agents 1, 3) + haiku-4.5 (Agents 2, 4). Mixed tiers | minimax-m2.5 for all swarm workers + sonnet-4.6 for synthesis. Clear separation | Hive (cleaner model strategy) |
| **Triage models** | haiku-4.5 for both Agents 5 and 6 (two sequential calls) | Merged into Layer 2 synthesis (single sonnet-4.6 call) | Hive (better model, fewer calls) |
| **Agent count** | 6 agents (Agents 1-6) | 1 deterministic + N swarm workers + 1 synthesizer | Depends on repo size |
| **Cost (medium repo, 100 files)** | ~$0.30-0.50 for 6 invocations (2 minimax + 4 haiku) | ~$0.35-0.55 for 25 invocations (24 minimax + 1 sonnet) | Original is slightly cheaper |
| **Cost (small repo, <50 files)** | ~$0.30-0.50 (same 6 invocations regardless of size) | ~$0.15-0.25 (6 minimax + 1 sonnet, much less context per worker) | Hive |
| **Latency** | Sequential bottleneck on Agent 1 (~30-60s), then parallel Agents 2-4 | Layer 0 is near-instant. Then all workers parallel. Then single synthesis | Hive (no serial LLM bottleneck) |
| **Failure mode** | Bad CodebaseMap from Agent 1 cascades to all downstream agents | Deterministic graph is reliable. Swarm findings are redundant (multiple workers per concern). Synthesis validates | Hive (more resilient) |
| **Finding quality** | Each concern analyzed once by a single agent | Each segment analyzed by 3 workers across 2 waves. MoA collaborativeness effect amplifies quality | Hive (research-backed) |
| **Simplicity** | 6 well-defined agents, easy to reason about | More moving parts: graph builder, segmenter, N workers, wave orchestration, synthesizer | Original is simpler |
| **Debuggability** | Each agent produces a single artifact file | Graph state between waves + per-worker outputs + synthesis output. More artifacts to inspect | Original is easier to debug |
| **Schema compatibility** | Native — ForgeResult, AuditFinding, RemediationPlan are the spec | Must produce identical output schemas. Layer 2 synthesis must output exact same types | Requires careful mapping |
| **Dependency footprint** | No additional dependencies beyond LLM providers | Adds Tree-sitter (AST parsing) + community detection algorithm | Original is lighter |
| **Incremental adoption** | N/A — current architecture | Feature flag `config.discovery_mode = "swarm" | "classic"`. Can A/B test | Hive (safe migration) |

### Where Original Spec Wins

1. **Simplicity.** 6 agents with clear responsibilities vs a graph-builder + segmenter + dynamic worker pool + wave orchestrator + synthesizer. More moving parts means more surface area for bugs.

2. **Cost on medium repos.** The original spec runs 6 invocations total. Hive runs ~25 for a medium repo. Even though individual minimax calls are cheap, the aggregate is slightly higher.

3. **Debuggability.** Each original agent produces one artifact file (`codebase_map.json`, `security_findings.json`, etc.). Hive produces graph state snapshots, per-worker outputs, wave diffs, and a synthesis summary. More to inspect when something goes wrong.

4. **No new dependencies.** The original spec needs only LLM API access. Hive adds Tree-sitter as a build dependency.

### Where Hive Discovery Wins

1. **No single point of failure.** Agent 1's minimax-m2.5 CodebaseMap is the foundation for everything. If it misses a module or mischaracterizes a dependency, all downstream agents inherit that blind spot. Hive's deterministic Layer 0 produces a reliable structural graph that doesn't depend on LLM accuracy for indexing.

2. **Cross-segment pattern detection.** The original architecture cannot detect that a security issue in auth combined with a quality issue in the API layer creates a critical vulnerability chain. Hive's shared graph and Wave 2 re-analysis make these cross-cutting patterns visible.

3. **Research-backed quality improvement.** MoA demonstrates that multiple cheap models in a layered arrangement outperform single expensive models. The two-wave pattern with shared context is a direct application of this finding.

4. **Better model allocation.** The original spec uses haiku-4.5 for triage (Agents 5-6) — a planning task where model quality matters. Hive concentrates the expensive model (sonnet-4.6) at the synthesis step where cross-referencing and judgment are critical, and uses cheap models for the distributed grunt work where they excel.

5. **Latency.** Agent 1 is a serial bottleneck (~30-60s). Layer 0 is deterministic and near-instant. Workers start immediately after.

---

## Shared Graph Memory: Implementation

Not a full graph database (Neo4j). For FORGE's use case, an in-memory graph serialized to JSON between waves is sufficient.

```python
class CodeGraph:
    """Shared context bus for swarm workers."""

    nodes: dict[str, GraphNode]      # id -> node (file, function, finding, etc.)
    edges: list[GraphEdge]           # (source_id, target_id, relationship, metadata)
    segments: list[Segment]          # clustered file groups

    def query_segment(self, segment_id: str) -> SegmentContext:
        """Get all nodes/edges for a segment + its graph neighbors."""

    def query_neighbors(self, segment_id: str, depth: int = 1) -> list[GraphNode]:
        """Get findings from neighboring segments (for Wave 2)."""

    def add_finding(self, finding: Finding, segment_id: str, affected_nodes: list[str]):
        """Worker writes a finding, linked to graph nodes."""

    def get_enriched_graph(self) -> dict:
        """Synthesis agent reads the full graph as structured JSON."""
```

Workers receive their segment's subgraph + neighbor findings as prompt context. No database, no external service — just structured data passing through the graph abstraction.

---

## Agent Mapping: Current --> Proposed

| Current Agent | Current Model | Proposed Replacement | Proposed Model |
|--------------|---------------|---------------------|----------------|
| Agent 1 (Codebase Analyst) | minimax-m2.5 | Layer 0 (deterministic) + Layer 1 workers collectively | No LLM for graph; minimax-m2.5 for workers |
| Agent 2 (Security Auditor) | haiku-4.5 | Layer 1 Security Workers (1 per segment x 2 waves) | minimax-m2.5 |
| Agent 3 (Quality Auditor) | minimax-m2.5 | Layer 1 Quality Workers (1 per segment x 2 waves) | minimax-m2.5 |
| Agent 4 (Architecture Reviewer) | haiku-4.5 | Layer 1 Architecture Workers (1 per segment x 2 waves) | minimax-m2.5 |
| Agent 6 (Triage Classifier) | haiku-4.5 | Merged into Layer 2 Synthesizer | sonnet-4.6 |
| Agent 5 (Fix Strategist) | haiku-4.5 | Merged into Layer 2 Synthesizer | sonnet-4.6 |

---

## Cost Estimation

### Medium Repo (100 files, 4 segments)

| Component | Invocations | Model | Est. Cost |
|-----------|------------|-------|-----------|
| Layer 0 (deterministic) | 0 LLM calls | N/A | $0.00 |
| Layer 1 Wave 1 | 12 (4 seg x 3 workers) | minimax-m2.5 | ~$0.07 |
| Layer 1 Wave 2 | 12 (4 seg x 3 workers) | minimax-m2.5 | ~$0.09 (larger context) |
| Layer 2 Synthesis | 1 | sonnet-4.6 | ~$0.15 |
| **Total** | **25** | | **~$0.31** |

### Small Repo (<50 files, 1 segment)

| Component | Invocations | Model | Est. Cost |
|-----------|------------|-------|-----------|
| Layer 0 (deterministic) | 0 | N/A | $0.00 |
| Layer 1 Wave 1 | 3 | minimax-m2.5 | ~$0.02 |
| Layer 1 Wave 2 | 3 | minimax-m2.5 | ~$0.03 |
| Layer 2 Synthesis | 1 | sonnet-4.6 | ~$0.10 |
| **Total** | **7** | | **~$0.15** |

### Large Repo (300 files, 7 segments)

| Component | Invocations | Model | Est. Cost |
|-----------|------------|-------|-----------|
| Layer 0 (deterministic) | 0 | N/A | $0.00 |
| Layer 1 Wave 1 | 21 | minimax-m2.5 | ~$0.13 |
| Layer 1 Wave 2 | 21 | minimax-m2.5 | ~$0.17 |
| Layer 2 Synthesis | 1 | sonnet-4.6 | ~$0.20 |
| **Total** | **43** | | **~$0.50** |

**Cost lever:** If budget is tight, skip Wave 2 entirely (drops cost by ~40%) or reduce to 2 workers per segment (security + quality only, skip separate architecture worker).

---

## Migration Path

### Phase 1: Build Layer 0 (Code Graph Builder)
- Add Tree-sitter AST parsing
- Implement community detection for segmentation
- Create `CodeGraph` data structure
- **Test:** Run on 5-10 real repos, verify segments make sense
- **Files:** `forge/graph/builder.py`, `forge/graph/models.py`

### Phase 2: Build Layer 1 (Swarm Workers)
- Create generic `SwarmWorker` base with graph read/write
- Implement SecurityWorker, QualityWorker, ArchitectureWorker
- Implement Wave 1 -> Wave 2 orchestration
- **Test:** Compare findings count and quality vs current Agents 2-4 on same repos
- **Files:** `forge/swarm/worker.py`, `forge/swarm/orchestrator.py`

### Phase 3: Build Layer 2 (Synthesizer)
- Single agent that reads enriched graph, produces CodebaseMap + Findings + RemediationPlan
- Replaces current Agents 1, 5, and 6
- **Test:** A/B compare synthesis output vs current pipeline output
- **Files:** `forge/swarm/synthesizer.py`

### Phase 4: Integration
- Wire into `forge/app.py` `discover()` and `remediate()` entry points
- Ensure ForgeResult schema compatibility
- Feature flag: `config.discovery_mode = "swarm" | "classic"`
- **Test:** Full pipeline end-to-end, verify remediation agents work with new discovery output

---

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Swarm produces more findings but lower quality (noise) | Medium | High | Synthesis layer deduplicates + confidence scoring; A/B test against classic |
| Tree-sitter parsing fails on unusual languages/frameworks | Medium | Medium | Fallback to file-based segmentation (no AST, just directory structure) |
| Two-wave latency exceeds current discovery time | Low | Medium | Wave 2 is optional; can skip for speed |
| Graph serialization bloats worker prompt context | Medium | Medium | Cap graph context per worker; use summary nodes for large segments |
| Minimax workers produce too many false positives without Haiku upgrade | Medium | Medium | Sonnet synthesis layer filters aggressively; monitor false positive rate |
| Single sonnet-4.6 synthesis call is a new cost center | Low | Low | Bounded at ~$0.15-0.20 per run; worth it for quality gate |

---

## Decisions Log

1. **Swarm worker models: Minimax M2.5.** The whole thesis is that architecture (shared graph + two-wave MoA pattern) compensates for model capability. Upgrading workers to Haiku would undermine the experiment.

2. **Synthesizer model: Sonnet 4.6.** The research (DisCIPL, COPE, MoA) consistently shows the aggregation/synthesis step is where model quality matters most. One strong call at the end is more impactful than spreading budget across workers.

3. **Scope: Discovery/Triage only.** Remediation already uses Sonnet 4.6 for coders (non-negotiable per spec). Swarm patterns add the most value where cheap models need to punch above their weight.

4. **Feature flag for safe rollout.** `config.discovery_mode = "swarm" | "classic"` allows A/B testing and instant rollback.
