## Problem and outcome

<!-- Describe the current observable behavior, desired outcome, and non-goals. -->

## Implementation

<!-- Map the important changes to the outcome. -->

## Validation

| ID | Observable acceptance criterion | Command or manual step | Environment | Expected | Observed | Status |
|---|---|---|---|---|---|---|
| V1 |  |  |  |  |  | PASS / FAIL / NOT VERIFIED |

## Platform evidence

<!--
List every real platform touched. Include adapter/upstream versions and last-verified evidence.
Write NOT VERIFIED when no real account/environment was used. Never attach credentials or personal data.
-->

## Risk, migration, and rollback

<!-- Include irreversible external effects, database/config changes, and rollback steps. -->

## Checklist

- [ ] I kept `AUTO_PUBLISH_ENABLED=false` for local/CI validation unless real publication was explicitly required.
- [ ] I added or updated tests for user-visible behavior.
- [ ] I ran the checks listed in `CONTRIBUTING.md` and reported only actual results.
- [ ] I updated `CHANGELOG.md` under `Unreleased`, or explained why there is no user-visible change.
- [ ] I updated the platform capability matrix if support evidence changed.
- [ ] I removed credentials, personal data, absolute private paths, private endpoints, and generated artifacts.
