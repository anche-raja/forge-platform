---
id: ejb3-to-spring
version: 0.1.0
title: EJB 3.x (@Stateless/@MDB) -> Spring
tier: framework
status: detect-only
detect:
  any:
    - import_prefix: "javax.ejb.Stateless"
    - import_prefix: "jakarta.ejb.Stateless"
    - import_prefix: "javax.ejb.MessageDriven"
applies_to:
  - selector: ejb_components
context: ejb_bean_table
depends_on: [spring-to-spring6]
decisions: [persistence]
eliminates: []
acceptance:
  - no_match: '(javax|jakarta)\.ejb\.'
    scope: "src/**/*.java"
---

## transform

**DETECT-ONLY.** This pack recognises the technology but does not yet transform it.

When discovery activates this pack, the engine records every matching file as
`MANUAL_REVIEW / reason=pack-not-implemented` and the migration report lists them under
"Detected but not migrated", with the evidence that triggered detection. The build gate will fail
on these files until they are migrated by hand or this pack is completed.

This is deliberate. Silently ignoring a technology that is present produces a migration that looks
complete and is not; naming the gap is what makes the plan honest at enterprise scale.

To complete this pack: replace this section with transform rules and the section below with a
rubric whose weights total 100, then set `status: complete` and bump `version` to 1.0.0.

## review

**DETECT-ONLY.** No rubric yet. Files matched by this pack never reach a reviewer.
