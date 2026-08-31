# Floati node workspace layout v0

Status: **DRAFT - architect copy and integration gate required**

## Convention

Every node working folder created by Floati is nested beneath its explicit
fleet root:

```text
<root>/nodes/<node-id>/
```

The node id uses the existing Floati identifier grammar. Floati never derives a
workspace from the user's home directory, never creates a sibling of the fleet
root, and never interprets a node id as a path.

## Lifecycle

Registration may opt into workspace creation. The option creates only the
`nodes` directory and the exact node leaf, both private by default. Existing
directories are accepted; symlinks, files, and other non-directory collisions
refuse before the registry append.

Retirement appends the existing registry retirement record and reports whether
the conventional workspace is present. It never deletes, empties, archives, or
moves that folder. Working bytes outlive node membership unless the operator
manages them separately.

## Local diagnostics

Doctor awareness compares active registry nodes with immediate directories
under `<root>/nodes`. It reports active nodes with missing or invalid folders
and inactive folders as orphans. Inspection is read-only and does not repair or
create a coordinate.

## Explicit exclusions

Existing workspaces are not migrated in v0. Floati does not search the home
directory, infer ownership of old folders, delete orphan folders, or rewrite a
registered node solely because its conventional folder is absent.
