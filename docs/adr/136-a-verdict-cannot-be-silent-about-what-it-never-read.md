# 136. A verdict cannot be silent about what it never read

## Status

Accepted (v1.16.61)

## Context

`diagnose_host` returns one word — `healthy`, `degraded`, `down` — and an
action line. Readers act on that word. It was computed from four signals: agent
reachability, traffic measured against baseline, IP rotation history, and the
count of open problems.

CPU was not among them.

So a host whose processors were fully occupied by a long-running process
satisfied every arm of the check and came back
`healthy` / **"No issues detected."** The traffic arm did not rescue it: a
compute-bound process moves almost no bytes, so egress read normal, and the
seasonal comparison confirmed it was normal. Every check ran, every check
passed, and none of them was looking at the thing that was wrong.

The failure is in the sentence, not only the logic. "No issues detected" is a
claim about the host. What the code could support was "no issues among the four
things I examined". Those differ precisely when the problem is in the fifth.

This is the same defect as ADR 133, where the absence of a traffic measurement
was asserted as traffic flowing, and ADR 128, where a check no host carried was
reported as healthy. Each time, a gap in coverage was rendered as a finding of
health. Here it happened one signal further out: not a value misread, but a
value never fetched.

### Why a threshold would not have been enough

The obvious fix — alert above some CPU percentage — has a structural blind
spot, and adding it alone would have produced a fix that still missed the case
that motivated it.

A process occupying a fixed number of cores occupies a fixed *fraction* of the
machine. Four busy cores are 100% of a four-core host and 50% of an eight-core
one. The identical workload is an emergency or is invisible depending on
hardware it does not control, and anything calibrated to sit below the line
stays below it indefinitely. A threshold answers "is this host busy", which is
not the same question as "is something wrong here".

What separates a pinned host from a merely busy one is **variance**. Real load
breathes: it has a daily shape, it responds to demand, its hourly minimum and
maximum differ by several points. A process that simply runs produces an hourly
min and max within a hair of each other, hour after hour, with no diurnal shape
at all. Measured on a real series, a pinned host held an hourly spread of about
half a percentage point across every hour it ran.

Flatness is threshold independent. That is the whole point: it is visible at
levels no alert would fire on.

## Decision

**CPU is judged, and the verdict says so.** `_classify_verdict` takes a CPU
flag and note. A host that is flat or busy is `degraded` rather than `healthy`,
and the action names which.

**`healthy` names the checks that produced it.** The action line now reads "No
issues detected in the checks that ran — agent reachability, traffic against
baseline, and active problems", followed by the CPU note. A reader can see the
boundary of the claim without reading the source.

**Unmeasured CPU is an answer, not a silence.** A host with no utilisation item
reports that CPU was not checked. A host with no CPU item is not a host with
acceptable CPU, and collapsing those two into the same output is how this
started.

**Flat outranks busy.** A host at 95% that varies is doing work. A host pinned
to a hair's breadth for hours is doing one thing repeatedly, which is both more
specific and more actionable.

**An item that never collected is not a measurement of zero.** Zabbix marks
one with `lastclock = 0`; read without checking the clock it renders as a real
value, and a load average of `0` timestamped 1970-01-01 reads as an idle host.
`cpu_pct_from_items` honours the sentinel and returns "no value".

The logic lives in `zbbx_mcp/cpu_load.py` as pure functions —
`cpu_pct_from_items`, `flat_run_hours`, `judge_cpu` — because `diagnose.py` is
at its size budget and because a signature detector deserves tests of its own
rather than only being exercised through a verdict.

## Consequences

The CPU **level** is free: it comes from the item list `diagnose_host` already
fetches, so no extra call. The **flat run** needs hourly trends, so it rides on
the existing `seasonal` flag — set on the single-host path, which already opts
into one extra read, and off for `bulk_diagnose`, which fans out. Bulk therefore
judges level but not flatness; that is a deliberate cost trade and not an
oversight.

Thresholds are judgement, not measurement: busy at 85%, flat at a spread under
2 percentage points sustained 6 hours above a 20% floor. The floor matters — an
idle host is perfectly flat too, and reporting that would be noise. The 6-hour
minimum keeps a steady batch job from qualifying. All are tunable constants.

A flat run is deliberately **not** an accusation. Constant-rate work has
innocent explanations — a transcode, a long build, a stuck loop. The tool
reports the shape and asks for the process to be identified; it does not label
the cause, which it cannot see.

Verified against real trend series: a long pinned window classifies `flat`,
while the same host once that load ended, and an unrelated lightly-loaded host,
both stay silent. A gap in the hourly data ends a run rather than being skipped, so two
separate runs are never spliced into one duration that never happened.
