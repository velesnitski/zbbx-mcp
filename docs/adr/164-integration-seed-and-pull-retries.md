# 164. Integration seed feeds the trigger until it fires; image pulls retry

## Status

Accepted (v1.18.0)

## Context

The end-to-end job failed intermittently in its seed step with "The seeded
trigger never produced a problem", on commits that passed on other runs.
The seed creates the hosts and items, then the trigger, then pushes values.
`push_values` already retries until the server knows every item, but the
trigger reaches the server's configuration cache separately: a value
accepted after its item is cached and before its trigger is, is stored and
never evaluated, and a `last()` trigger re-evaluates only on a new value. One
push could therefore leave the problem unopened for good. Running the job on
three releases at once (ADR 162) made the race visible on every run.

Separately, image pulls failed on registry 502s, failing a run before any
test executed.

## Decision

- `wait_for_problem` takes a `repush` callable and calls it every ten seconds
  while waiting; the seed passes one that pushes a fresh value for the
  trigger's item. The first value the cached trigger sees opens the problem.
  The wait is three minutes.
- The workflow pulls the images in their own step, retried four times with a
  growing pause, before starting the stack.

## Test approach

Unit tests with a fake server that opens a problem only for a value pushed
after its trigger is cached: the loop pushes until it fires, does not push
when the problem is already open, and still reports the failure when nothing
feeds it. These run in the normal suite, without Docker.

## Consequences

A seed failure now means the trigger really does not fire, and a pull
failure means the registry stayed down for about two and a half minutes.
