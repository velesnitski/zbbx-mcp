# 155. Journal after the write succeeds

## Status

Accepted (v1.16.76)

## Context

The rollback journal (ADR 008) is only useful if every entry describes a
change that actually happened, and every change that happened has an entry.
Writing the wire tests of ADR 150 found both halves broken.

- The shared path snapshotted and journalled in one step, before the write.
  A delete or update that failed left an entry for a change that never
  happened, and a later `rollback_last` would re-create or overwrite an
  object that was never touched.
- `rollback_last` removed the entry before attempting the undo, so a failed
  undo lost the entry for good.
- `link_template` sent `templates` on `host.update`, which replaces the
  host's whole template list rather than adding one, and journalled nothing.
- `unlink_template` sent `templates_clear` on both branches, so asking to
  keep inherited items deleted them anyway, and journalled nothing.
- `set_bulk_macro` journalled nothing and printed the new value even when it
  overwrote a secret macro.
- Never-collected items rendered their clock as 1970.

## Decision

**Snapshot before, journal after.** `client.take_snapshot()` runs before a
write and returns the prior state (secret macros redacted as before);
`client.journal()` records the entry only once the write has returned. Every
update and delete tool uses the pair; a failed write leaves no entry.

**An entry leaves the journal only when its undo succeeds**, for
`rollback_last` as already for `rollback_by_index`.

**Template links add and remove.** Linking uses `host.massadd`; unlinking
uses `host.massremove`, with `templateids_clear` only when the caller asks
to clear. Both are journalled and undoable; undoing a clearing unlink
relinks and says that the deleted history is not restored.

**Bulk macro writes are journalled per host**, and a value is shown as
`<secret>` whenever the macro being written is secret.

**A never-collected item reads "never"** in the item listings.

## Consequences

Callers see one behavioural change on purpose: linking a template no longer
drops the host's other templates. An entry whose undo cannot run now stays
at the top of the journal until removed by index; whether to add an explicit
discard is left open.
