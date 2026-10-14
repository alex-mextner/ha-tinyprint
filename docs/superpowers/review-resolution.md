# Initial review resolution

Three-role review completed through the normal review-cli Claude CLI route.
The default hard-coded provider roster failed; the supported claude:sonnet alias
worked without changing global settings or bypassing hooks.

Addressed with regression tests:
- Printing owns its own task; integration unload becomes a service error without
  cancelling the caller script. Caller cancellation still stops and cleans the job.
- Connected-client scope releases an assigned or late connection result, including
  cancellation during the handoff. Transport remains responsible for notification cleanup.
- Default transfer budget scales by output rows and remains bounded at 600 seconds.
  Kept the conservative 20-byte chunk size: a larger MTU is not proof that this
  printer firmware accepts larger write chunks.
- Invalid YAML import aborts instead of leaving an unattended form.
- Invalid image data is an input validation error, not a Bluetooth failure.

No claimed physical completion: tests exercise software contracts; actual paper
output still requires the target printer to advertise and a visible readable ticket.
Style-only framework rewrites were deferred to keep this experimental integration
small; no existing Home Assistant configuration is refactored by this change.
