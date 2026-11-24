"""Conservative source tracing for the fixed-parent test hazard inventory.

Native objects supply class identity and inherited methods. Only test-source
helpers are followed; this is not a product call-graph analyzer. Import aliases,
qualified calls, simple callable aliases, self helpers, and helper constructors
are supported. Relevant dynamic lookup or unavailable source refuses review.
No fixture body is executed by this module.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
import sys
import types
import unittest


_FIXTURE = "tests.test_workers._EffectWorkerCase"
_ROOT = "floati.host_paths.worker_workspace_root"

# Exact source callables, not whole classes: another direct root caller needs
# review even if added to an already-reviewed creator/coordinate-only class.
_REVIEWED_ROOT_CALLERS = {
    "tests.bus_scale_commands._baseline_state": "Platform example-token coordinate only; does not inspect or create the fixed work parent.",
    "tests.test_codex_live_adapter.CodexAppServerAdapterTests.setUp":
        "UUID child; real adapter creates/validates parent with exist_ok and cleanup removes only its child.",
    "tests.test_pi_adapter.PiRpcAdapterTests.setUp":
        "UUID child; real adapter creates/validates parent with exist_ok and cleanup removes only its child.",
    "tests.test_spawn_groups.SpawnGroupFixtures.admitted_record": "Record coordinate only.",
    "tests.test_spawn_groups.SpawnGroupFixtures.admitted": "Record coordinate comparison only.",
    "tests.test_spawn_groups.SpawnGroupAdmissionTests.test_child_admission_attenuates_group_and_run_evidence": "Record coordinate comparison only.",
    "tests.test_spawn_groups.SpawnGroupRecordTests.test_child_admitted_accepts_only_floati_workspace_in_runtime_and_schema": "Record/runtime/schema coordinate control only.",
    "tests.test_cli_workflows.CliWorkflowTests.test_work_add_workspace_records_the_exact_derived_live_mapping": "Work-add records a derived coordinate; it does not create the directory.",
    "tests.test_framing_work.WorkLogTests.test_workspace_mapping_is_derived_from_the_new_work_id_and_legacy_items_remain_readable": "Work-add records a derived coordinate; it does not create the directory.",
    "tests.test_schemas.SchemaContractTests.test_work_items_and_transitions_are_distinct_orchestration_truth": "Schema coordinate data only.",
    "tests.test_schemas.SchemaContractTests.test_work_item_runtime_accepts_only_the_floati_workspace_root": "Runtime record-validation coordinate data only.",
    "tests.test_schemas.SchemaContractTests.test_work_item_schema_accepts_only_the_floati_workspace_root": "Schema coordinate data only.",
    "tests.test_approval_suspension.ApprovalSuspensionProjectionTests.suspension_record": "Durable record coordinate only.",
    "tests.test_approval_suspension.ApprovalSuspensionProjectionTests.replay_floati_workspace": "Record coordinate comparison only.",
    "tests.test_approval_suspension.ApprovalSuspensionProjectionTests.test_consumption_repeats_every_suspension_coordinate_and_uses_newer_same_subject_authority": "Hostile record coordinate only.",
    "tests.test_approval_suspension.ApprovalSuspensionProjectionTests.test_suspension_runtime_and_schema_contracts_match": "Runtime/schema record coordinate control only.",
    "tests.test_worker_workspace_portability.WorkerWorkspacePortabilityTests.test_worker_workspace_root_follows_the_patched_host_platform": "Pure host-path selection with a process-local platform patch.",
    "tests.test_worker_workspace_portability.HostTempRootTests.test_override_moves_both_roots_beneath_the_owned_directory": "Pure host-path selection under a process-local override; creates nothing.",
    "tests.test_worker_workspace_portability.HostTempRootTests.test_invalid_override_refuses_and_never_falls_back": "Refusal control under a process-local override; creates nothing.",
    "tests.test_worker_workspace_portability.HostTempRootTests.test_override_too_long_for_a_socket_refuses_only_the_endpoint": "Pure host-path selection beneath its own fixture; creates no work parent.",
    "tests.test_worker_workspace_portability.HostTempRootTests.test_suite_and_cli_children_share_the_suite_owned_root": "Reads the suite-owned coordinate; its CLI children only init and read status.",
}


def _identity(value):
    if not (inspect.isfunction(value) or inspect.isclass(value)):
        return ""
    return (value.__module__ or "") + "." + value.__qualname__


def _fixture(value):
    return inspect.isclass(value) and any(_identity(base) == _FIXTURE for base in value.__mro__)


def _attribute(value, name):
    if isinstance(value, types.ModuleType) or inspect.isclass(value):
        result = inspect.getattr_static(value, name, None)
        result = result.__func__ if isinstance(result, (staticmethod, classmethod)) else result
        return inspect.unwrap(result) if inspect.isfunction(result) else result
    return None


def fixed_parent_consumers(suite, *, source_root=None):
    """Return native class names needing serial rules; refuse unreviewed roots.

    Traversal may conservatively include an unused nested body or helper method.
    That can require review, but cannot silently discard a source dependency.
    """
    classes = {}
    source_root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]

    def collect(node):
        if isinstance(node, unittest.TestSuite):
            for child in node:
                collect(child)
        elif isinstance(node, unittest.TestCase):
            classes.setdefault(type(node), set()).add(node._testMethodName)
        else:
            raise ValueError("unsupported native test object in fixed-parent census")

    collect(suite)
    consumers = set()
    parsed = {}
    relevant_modules = {}
    attributes = {}
    sources = {}
    syntax = {}
    class_methods = {}

    # Discovery supplies stable native objects; no fixture body runs during
    # this traversal. Share their syntax and static metadata across classes,
    # while retaining each class's own owner-dependent dependency walk.
    def attribute(value, name):
        if not (isinstance(value, types.ModuleType) or inspect.isclass(value)):
            return None
        key = value, name
        if key not in attributes:
            attributes[key] = _attribute(value, name)
        return attributes[key]

    def source(value):
        if value not in sources:
            sources[value] = inspect.getsourcefile(value)
        return sources[value]

    def source_is_relevant(name):
        if name in relevant_modules:
            return relevant_modules[name] is not False  # Cycles stay conservative.
        relevant_modules[name] = None
        path = source_root / (name.replace(".", "/") + ".py")
        if not path.is_file():
            return True  # Missing source is not evidence of independence.
        tree = ast.parse(path.read_text())
        relevant = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("_EffectWorkerCase", "worker_workspace_root", "__import__"):
                relevant = True
            elif isinstance(node, ast.Attribute) and node.attr in ("_EffectWorkerCase", "worker_workspace_root", "import_module"):
                relevant = True
            elif isinstance(node, ast.Import):
                if any(item.name == "floati.host_paths" or item.name.startswith("tests.") and source_is_relevant(item.name) for item in node.names):
                    relevant = True
            elif isinstance(node, ast.ImportFrom):
                if node.level or node.module == "floati.host_paths" or node.module == "floati" and any(item.name == "host_paths" for item in node.names):
                    relevant = True
                if node.module and node.module.startswith("tests"):
                    imported = [node.module] if node.module != "tests" else ["tests." + item.name for item in node.names]
                    if any(source_is_relevant(item) for item in imported):
                        relevant = True
        relevant_modules[name] = relevant
        return relevant

    def function_tree(value, path):
        if path not in parsed:
            parsed[path] = {}
            for node in ast.walk(ast.parse(Path(path).read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = min([node.lineno, *(item.lineno for item in node.decorator_list)])
                    parsed[path][first, node.name] = node
        line = value.__code__.co_firstlineno
        if (line, value.__name__) in parsed[path]:
            return parsed[path][line, value.__name__]
        raise ValueError("unavailable test-helper source: " + _identity(value))

    def function_syntax(value, path):
        if value not in syntax:
            tree = function_tree(value, path)
            nodes = tuple(ast.walk(tree))
            parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
            syntax[value] = nodes, parents, inspect.signature(value).parameters
        return syntax[value]

    for case, test_methods in classes.items():
        visited = set()
        effects, roots = set(), set()
        source_files = {
            source(attribute(case, name))
            for name in test_methods if inspect.isfunction(attribute(case, name))
        }

        def follow(value, owner=case):
            if _fixture(value):
                effects.add(_identity(value))
                return
            if inspect.isclass(value):
                if (value, "class") in visited:
                    return
                visited.add((value, "class"))
                if value.__module__ == "builtins":
                    return
                # A test helper can defer fixture construction until a method.
                if value not in class_methods:
                    class_methods[value] = tuple(
                        candidate for cls in value.__mro__ for name in vars(cls)
                        if inspect.isfunction(candidate := attribute(cls, name)))
                for candidate in class_methods[value]:
                    follow(candidate, value)
                return
            if not inspect.isfunction(value):
                return
            value = inspect.unwrap(value)
            module_name = value.__globals__.get("__name__", "")
            if module_name.startswith("tests.") and not source_is_relevant(module_name):
                return
            path = source(value)
            if path not in source_files and not (path and "tests" in Path(path).parts):
                return
            key = (value, owner)
            if key in visited:
                return
            visited.add(key)
            try:
                nodes, parents, parameters = function_syntax(value, path)
            except (OSError, TypeError, SyntaxError) as exc:
                raise ValueError("unavailable test-helper source: " + _identity(value)) from exc
            environment = dict(value.__globals__)
            if parameters:
                first = next(iter(parameters))
                if first in ("self", "cls"):
                    environment[first] = owner

            def module(name):
                result = sys.modules.get(name)
                if result is None and name.startswith("tests.") and source_is_relevant(name):
                    raise ValueError("unloaded test helper requires review: " + name)
                return result

            def resolve(node):
                if isinstance(node, ast.Name):
                    return environment.get(node.id)
                if isinstance(node, ast.Attribute):
                    return attribute(resolve(node.value), node.attr)
                if isinstance(node, ast.Call):
                    target = resolve(node.func)
                    if target is __import__ or inspect.isfunction(target) and target.__name__ == "import_module":
                        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                            return module(node.args[0].value)
                    if isinstance(node.func, ast.Name) and node.func.id == "getattr" and node.args:
                        target = resolve(node.args[0])
                        if isinstance(target, types.ModuleType) and target.__name__ in ("tests.test_workers", "floati.host_paths") or _fixture(target):
                            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                                return attribute(target, node.args[1].value)
                            raise ValueError("dynamic fixed-parent lookup requires review: " + _identity(value))
                    if inspect.isclass(target):
                        return target
                return None

            for node in nodes:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        environment[alias.asname or alias.name.split(".")[0]] = module(alias.name if alias.asname else alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        raise ValueError("relative test-helper import requires review: " + _identity(value))
                    imported = module(node.module or "")
                    for alias in node.names:
                        if alias.name == "*":
                            raise ValueError("star test-helper import requires review: " + _identity(value))
                        environment[alias.asname or alias.name] = attribute(imported, alias.name)
            for node in nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    target = resolve(node.value) if node.value is not None else None
                    if _fixture(target):
                        effects.add(_identity(target))
                    destinations = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for destination in destinations:
                        if isinstance(destination, ast.Name) and target is not None:
                            environment[destination.id] = target
                elif isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        if _fixture(resolve(base)):
                            effects.add(_FIXTURE)
            for node in nodes:
                if isinstance(node, (ast.Name, ast.Attribute)):
                    # A bare/stored constructor or callable can be invoked
                    # indirectly. Static event factories remain distinct.
                    parent = parents.get(node)
                    if not isinstance(parent, ast.Attribute) or parent.value is not node:
                        candidate = resolve(node)
                        if _identity(candidate) == _ROOT:
                            roots.add(_identity(value))
                        elif _fixture(candidate) or inspect.isfunction(candidate):
                            follow(candidate, owner)
                if isinstance(node, ast.Call):
                    target = resolve(node.func)
                    if _identity(target) == _ROOT:
                        roots.add(_identity(value))
                    else:
                        follow(target, owner)
                    # Passing a fixture constructor/helper as a callback cannot
                    # make its dependency disappear behind another callable.
                    for argument in (*node.args, *(item.value for item in node.keywords)):
                        candidate = resolve(argument)
                        if _fixture(candidate) or inspect.isfunction(candidate):
                            follow(candidate, owner)

        for name in ("setUpClass", "tearDownClass", "setUp", "tearDown", *sorted(test_methods)):
            follow(attribute(case, name))
        unknown = roots - _REVIEWED_ROOT_CALLERS.keys()
        if effects:
            consumers.add(_identity(case))
        elif unknown:
            raise ValueError("unreviewed fixed work parent use in " + _identity(case) + ": " + ", ".join(sorted(unknown)))
    return consumers
