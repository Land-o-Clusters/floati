A role template is a typed contract, not a personality. To author one:

1. **Fields.** `duties` (what the seat does), `decision_rights` (what it may
   decide alone), `stops` (when it must wait), `fences` (what it must never
   do), `cadence`, `questions` (what the operator must answer before the role
   binds). Every list entry is one sentence, plain ASCII, no trailing stamp.
2. **Voice.** Duties start with a verb. Fences start with "Never". Stops start
   with "Stop". One claim per sentence; a sentence an operator could misread
   is a defect.
3. **Questions.** Ask only what the template cannot know: the territory
   (`repo`), the hard boundary (`never_touch`), the reporting line
   (`reports_to`). Answers must exactly match declared questions — extra or
   missing answers refuse.
4. **Versioning.** Shipped templates are immutable at a version; any content
   change bumps `template_version` and re-digests. A role record binds the
   version and digest it was assigned under.
5. **Tests.** Add the file to the shipped-roster pin (the loader reads an
   exact roster, never a directory scan), validate against
   `role-template.schema.json`, and keep the no-DRAFT copy fence green.
