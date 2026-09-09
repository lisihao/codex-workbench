# Claude JSON envelope compatibility

`ClaudeExecutor` accepts the documented JSON result object and one compatible
event-array form.  This compatibility is limited to decoding an execution
result; it does not change executor routing, authentication, quota, scope, or
exit-code policy.

## Supported envelopes

The legacy envelope is a top-level object.  Its existing result validation is
unchanged.

The event-array envelope is a top-level array of event objects.  It is accepted
only when it has exactly one `type: "result"` event, that event is the final
array entry, and it has all of the following:

- `subtype: "success"`;
- `is_error: false` (the boolean value, not an omitted or truthy substitute);
- a dictionary `structured_output` that passes the existing worker-result
  schema validation; and
- one unambiguous actual-model attestation.  A single `modelUsage` model or the
  existing explicit model attestation can satisfy this; conflicting or multiple
  model claims are rejected.

The worker-result schema continues to preserve business status exactly:
`succeeded` remains `succeeded`, `blocked` remains `blocked`, and `failed`
remains `failed`.  A non-zero process exit still yields `failed`.

## Fail-closed behavior

The executor rejects empty or scalar JSON, malformed JSON, arrays containing a
non-object event, absent or multiple terminal results, a result that is not
final, terminal CLI errors or non-success subtypes, missing or invalid
structured output, and missing, ambiguous, or conflicting model attestations.
It never picks an arbitrary dictionary or a convenient last object from an
event array.

Process output artifacts remain the original process output.  Parser diagnostics
use only sanitized envelope labels and never include event message text.

## Evidence boundary

The upstream report [anthropics/claude-code#84784](https://github.com/anthropics/claude-code/issues/84784)
documents a reported interaction where `--verbose` can make `--output-format json`
emit an event array with a final result object.  The [Claude Code headless
documentation](https://code.claude.com/docs/en/headless) documents the ordinary
JSON result/metadata envelope and `structured_output`.  These sources motivate
compatibility handling only; they do not establish a local cause or prove that a
local verbose setting caused any particular execution.
