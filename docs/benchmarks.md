# SDK overhead baseline

Recorded 2026-10-12, Phase 2. This is a **baseline**, not an optimization result: nothing was
tuned for it. Raw results: `sdk/python/benchmarks/results/2026-10-12-py3.13.json` and
`…-py3.11.json`. Compare future runs only on the same machine.

## How to reproduce

```bash
uv run python sdk/python/benchmarks/overhead.py --json result.json      # full run, ~1 min
uv run python sdk/python/benchmarks/overhead.py --quick                 # smoke run, ~6 s
```

## Method

The harness is `sdk/python/benchmarks/overhead.py`; its docstring is the authoritative description.

* Each scenario runs an operation **without** instrumentation (baseline) and **with** it, in the
  same process, alternating baseline and instrumented repeats so machine drift hits both.
* 5,000 warm-up operations, then 15 repeats × 20,000 timed operations each, timed with
  `time.perf_counter_ns`; reported values are the **median and p90 across repeats** of ns/op.
* **Overhead = median(instrumented) − median(baseline)**, per operation.
* The exporter is an in-process null exporter: batches are encoded (JSON) and discarded, so the
  network and the collector are excluded — this measures the SDK only. Batches are flushed between
  repeats, outside the timed region (in production that work runs on the background thread).
* *Total CPU incl. export* is `time.process_time()` (all threads: application + exporter,
  including JSON encoding) for one pass of 20,000 instrumented operations plus a flush, minus the
  same for the baseline. It is a single pass, so it is noisier than the median columns.
* *Memory per queued event*: `tracemalloc` delta while 900 completed tool spans sit in the queue.
* GC stays enabled (as in production); `gc.collect()` runs before each timed repeat.
* Scenarios: an empty `aobs.span`; a `@aobs.tool` call (bound-argument fingerprint, metadata-only
  and full capture); a wrapped OpenAI-compatible `chat.completions.create` against an in-process
  fake client returning a fixed response with usage (~1 KB prompt; metadata-only and full capture);
  a decorated tool call with the SDK **not initialized**.
* Checked by `sdk/python/tests/test_sdk_overhead.py`: every scenario runs, no events are dropped
  (drops would distort the numbers), and generous regression bounds hold.

## Results — reference machine

Intel Xeon @ 2.10 GHz, 2 vCPUs (shared cloud VM), Linux 6.18.

### Python 3.13.16

| scenario | capture | baseline ns/op | instrumented ns/op (median) | p90 | **overhead µs/op** | total CPU µs/op incl. export | bytes/event |
|---|---|---|---|---|---|---|---|
| empty_span | metadata_only | 327 | 26,638 | 28,611 | **26.3** | 25.8 | 265 |
| tool_call_metadata_only | metadata_only | 111 | 57,040 | 64,954 | **56.9** | 58.0 | 504 |
| tool_call_full_capture | full | 112 | 73,319 | 80,417 | **73.2** | 67.7 | 619 |
| llm_call_metadata_only | metadata_only | 163 | 98,306 | 105,729 | **98.1** | 87.1 | 735 |
| llm_call_full_capture | full | 171 | 153,186 | 160,828 | **153.0** | 146.3 | 1,923 |
| tool_call_sdk_not_initialized | — | 112 | 794 | — | **0.68** | — | — |

Memory: **1,737 bytes per queued event**.

### Python 3.11.17

| scenario | capture | baseline ns/op | instrumented ns/op (median) | p90 | **overhead µs/op** | total CPU µs/op incl. export | bytes/event |
|---|---|---|---|---|---|---|---|
| empty_span | metadata_only | 262 | 26,093 | 28,564 | **25.8** | 23.9 | 265 |
| tool_call_metadata_only | metadata_only | 115 | 56,282 | 62,454 | **56.2** | 58.8 | 504 |
| tool_call_full_capture | full | 125 | 78,959 | 83,717 | **78.8** | 82.7 | 619 |
| llm_call_metadata_only | metadata_only | 163 | 105,258 | 112,931 | **105.1** | 119.0 | 735 |
| llm_call_full_capture | full | 158 | 163,719 | 166,249 | **163.6** | 179.3 | 1,923 |
| tool_call_sdk_not_initialized | — | 104 | 819 | — | **0.71** | — | — |

Memory: **1,793 bytes per queued event**.

## Reading the numbers

* Against the roadmap target (< 50 µs median per span): an **empty span meets it (~26 µs)**; a
  **decorated tool call does not (~56–57 µs)**, because it also binds arguments and computes the
  keyed fingerprint. Not met for tool calls on this machine.
* The baselines are trivial in-process calls (~0.1 µs), so the overhead is shown in isolation. As
  arithmetic only: a wrapped model call adds ~0.1 ms, which would be 0.02% of a 500 ms model call;
  a tool call adds ~0.06 ms. Real-workload measurements come with the Phase 8 end-to-end harness.
* With the SDK not initialized, instrumented code costs ~0.7 µs per call.
* Bytes per event are uncompressed JSON on the wire (with ~600 bytes of batch envelope shared by
  up to 500 events).

## Where the time goes (profile, not yet acted on)

`cProfile` of 20,000 metadata-only tool calls (Python 3.13; the profiler inflates absolute
times, the proportions are what matter): keyed fingerprint (canonical JSON + HMAC) ≈ 22% of the
instrumented call, `inspect` argument binding ≈ 17%, building the start and end event dicts, three
timestamp formats per span, and five diagnostics-counter lock acquisitions per span. No single
hotspot. Candidates, each to be measured with this harness before being adopted:

1. Build `span_start` events lazily on the worker thread (most are coalesced away and never sent).
2. Format the start timestamp once per span instead of three times.
3. Cache the decorated function's parameter metadata instead of a full `Signature.bind` per call.
4. Make fingerprinting of tool arguments optional per tool for very hot, low-value tools.
5. Lock-free (per-thread) diagnostics counters.
