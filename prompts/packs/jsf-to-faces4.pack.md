---
id: jsf-to-faces4
version: 0.1.0
title: JSF 1.x/2.x -> Jakarta Faces 4
tier: view
status: detect-only
detect:
  any:
    - file_glob: "**/faces-config.xml"
    - file_glob: "**/*.xhtml"
    - dependency: "com.sun.faces:jsf-api"
    - import_prefix: "javax.faces"
applies_to:
  - file_glob: "**/faces-config.xml"
  - file_glob: "**/*.xhtml"
  - selector: faces_backing_beans
context: faces_navigation_graph
depends_on: [javax-to-jakarta]
decisions: [views]
eliminates: []
acceptance:
  - no_match: 'javax\.faces\.'
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
