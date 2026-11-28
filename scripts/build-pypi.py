"""Build exactly the governed runtime inventory for Python distribution."""
import json
from pathlib import Path
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BundleBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root).resolve()
        # The backend does not put the project on its import path. Load the
        # checked-out verifier so the source and Python installers agree.
        sys.path.insert(0, str(root))
        try:
            from floati.manifest import MANIFEST_NAME, verify_manifest
            errors = verify_manifest(root)
        finally:
            sys.path.remove(str(root))
        if errors:
            raise ValueError("Cannot build a stale Floati bundle: " + "; ".join(errors))

        prefix = "floati/_runtime/" if self.target_name == "wheel" else ""
        manifest = root / MANIFEST_NAME
        inventory = json.loads(manifest.read_text(encoding="utf-8"))
        paths = [MANIFEST_NAME, *(row["path"] for row in inventory["files"])]
        build_data["force_include"].update({
            str(root / path): prefix + path for path in paths
        })
