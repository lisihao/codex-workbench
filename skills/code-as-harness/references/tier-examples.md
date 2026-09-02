# Verification tier examples

## L0

- explain a code path without edits;
- correct a README typo;
- change non-executable UI copy where no snapshot or localization contract applies.

Check: inspect the relevant source or diff.

## L1

- fix one parser branch;
- add one local setting field;
- adjust one component behavior with an existing focused test.

Check: the narrowest test or build target that executes that behavior.

## L2

- change a public response shape;
- touch several packages behind one feature;
- fix a shared scheduler, session format, or plugin contract without releasing it.

Check: affected suite plus type/lint/build or the repository's quick governance profile.

## L3

- deliver a DSH Desktop package;
- migrate a database or durable schema;
- prepare release, publish, merge-readiness, or deployment;
- change a security control or governance engine;
- perform destructive or persistent-state changes.

Check: the exact mandatory project protocol once stable, followed by required runtime and attestation evidence.
