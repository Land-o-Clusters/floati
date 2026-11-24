"""Derive bounded native representatives from the census harness's routing.

These are observation/transport paths, not product command implementations.
Every case-only comparison in the named orchestration functions contributes
to the signature; a new branch therefore creates a class automatically. The
lexicographically first command represents each class. No command is started.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/bus_scale_census"
ADAPTER = FIXTURES / "bus-scale-expanded-observation-2026-10-15.py"
ENTRY = FIXTURES / "bus-scale-native-entry-2026-10-09.py"
_HARNESS_FUNCTIONS = {
    ADAPTER: {"expanded_observation", "auxiliary_roots", "auxiliary_proof", "native_publication"},
    ENTRY: {"main", "prepared_stdin", "routine_subprocess", "node_prompt_source_git"},
}
# These calls cross from observation/transport orchestration into product
# arguments, adapter selection or evidence validation. Their command-specific
# implementations retain their separate bounded positive/negative controls.
_PRODUCT_BOUNDARIES = frozenset({
    "arguments", "routine_module", "witness", "refusal_witness", "normalize",
    "normalize_refusal", "_projection_time", "attention_categories",
    "validate_attention_proof", "materialize_attention_pair",
    "validate_functional_refusal", "bind_functional_refusal", "execute_seam",
})


def _literal(node, constants):
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "frozenset"} and len(node.args) == 1 and not node.keywords:
        return frozenset(_literal(node.args[0], constants))
    return ast.literal_eval(node)


def _constants(tree):
    values = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            try:
                value = _literal(statement.value, values)
            except (ValueError, TypeError):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
    return values


def _case_predicates(function, constants, functions):
    """Closed routing expressions plus recursively discovered local helpers.

    Boolean aliases retain their underlying comparisons. A scalar case alias,
    computed case guard or unknown case-taking callee refuses, rather than
    silently assigning a new transport to an existing representative.
    """
    result = []
    called = set()
    aliases = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                # Track selectors and Boolean guards, not arbitrary prepared
                # payloads returned by a case-taking fixture helper. In
                # particular seed_pair=attention_clock_pair(case,...) does
                # not turn seed_pair.get(...) into a computed case selector.
                # Called helpers are inspected separately below.
                selector=isinstance(node.value,(ast.Name,ast.BoolOp,ast.Compare,ast.UnaryOp,ast.BinOp))
                method=isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute)
                if isinstance(target, ast.Name) and target.id != "case" and (selector or method):
                    aliases[target.id] = node.value

    def is_case(node):
        return isinstance(node, ast.Name) and node.id == "case"

    def contains_case(node, seen=frozenset()):
        if is_case(node):
            return True
        if isinstance(node, ast.Name) and node.id in aliases and node.id not in seen:
            return contains_case(aliases[node.id], seen | {node.id})
        if isinstance(node, ast.Call):
            name=getattr(node.func,"id",getattr(node.func,"attr",None))
            if name in _PRODUCT_BOUNDARIES:
                return False
            if isinstance(node.func,ast.Attribute):
                # A scalar transformation is rooted at case itself. Passing
                # case as payload inside owner.launch_owned_child(...), then
                # collecting that child, does not make its result a selector.
                return contains_case(node.func.value,seen)
        return any(contains_case(child,seen) for child in ast.iter_child_nodes(node))

    def context_coordinate(node):
        # Native context/detail equality is input validation, not a new route.
        if isinstance(node, ast.Subscript):
            return isinstance(node.value, ast.Name) and node.value.id in {"context","detail"}
        return (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                and isinstance(node.func.value,ast.Name) and node.func.value.id=="context"
                and node.func.attr=="get")

    def visit_guard(node, seen=frozenset()):
        if isinstance(node, ast.Compare):
            if len(node.ops)==1 and is_case(node.left):
                try:
                    value=_literal(node.comparators[0],constants)
                except (ValueError,TypeError) as error:
                    raise ValueError("unsupported census case routing predicate: "+ast.dump(node)) from error
                operation=node.ops[0]
                if not isinstance(operation,(ast.Eq,ast.NotEq,ast.In,ast.NotIn)):
                    raise ValueError("unsupported census case routing comparison")
                result.append((type(operation).__name__,value))
                return
            if any(is_case(child) for child in [node.left,*node.comparators]):
                if len(node.ops)==1 and is_case(node.comparators[0]) and context_coordinate(node.left):
                    return
                raise ValueError("unsupported reversed or chained census case routing comparison")
        if isinstance(node, ast.Name):
            if node.id=="case":
                raise ValueError("unsupported implicit or aliased census case routing")
            if node.id in aliases and node.id not in seen and contains_case(aliases[node.id]):
                visit_guard(aliases[node.id],seen|{node.id})
            return
        if isinstance(node, ast.Call):
            direct=[argument for argument in [*node.args,*(keyword.value for keyword in node.keywords)]
                    if contains_case(argument)]
            name=getattr(node.func,"id",getattr(node.func,"attr",None))
            if isinstance(node.func,ast.Attribute) and contains_case(node.func.value):
                raise ValueError("unsupported computed census case routing: "+ast.dump(node))
            if direct:
                if name in _PRODUCT_BOUNDARIES:
                    return
                if isinstance(node.func,ast.Name) and name in functions and all(is_case(argument) for argument in direct):
                    called.add(name)
                    return
                raise ValueError("unresolved or unsupported census case routing helper: "+str(name))
            return
        for child in ast.iter_child_nodes(node):
            visit_guard(child,seen)

    for node in ast.walk(function):
        if type(node).__name__=="Match" and contains_case(node.subject):
            raise ValueError("unsupported match census case routing")
        if not isinstance(node,ast.Call):
            continue
        name=getattr(node.func,"id",getattr(node.func,"attr",None))
        if isinstance(node.func,ast.Name) and name in functions and name not in _PRODUCT_BOUNDARIES:
            called.add(name)
        direct=[argument for argument in [*node.args,*(keyword.value for keyword in node.keywords)]
                if is_case(argument)]
        if direct and name not in _PRODUCT_BOUNDARIES:
            if not isinstance(node.func,ast.Name) or name not in functions:
                raise ValueError("unresolved census case routing helper: "+str(name))
            # A renamed formal parameter would otherwise hide every guard.
            parameters=[argument.arg for argument in functions[name].args.args]
            for index,argument in enumerate(node.args):
                if is_case(argument) and (index>=len(parameters) or parameters[index]!="case"):
                    raise ValueError("unsupported aliased census case routing parameter")
            keyword_parameters=set(parameters)|{argument.arg for argument in functions[name].args.kwonlyargs}
            for keyword in node.keywords:
                if is_case(keyword.value) and (keyword.arg!="case" or "case" not in keyword_parameters):
                    raise ValueError("unsupported aliased census case routing keyword parameter")
    guards = [node.test for node in ast.walk(function) if isinstance(node, (ast.If, ast.IfExp))]
    guards += [node.body for node in ast.walk(function) if isinstance(node, ast.Lambda)]
    guards += [node.value for node in ast.walk(function) if isinstance(node,ast.Return)
               and node.value is not None and isinstance(node.value,(ast.Compare,ast.BoolOp,ast.UnaryOp))]
    for guard in guards:
        visit_guard(guard)
    return result,called


def _matches(case, predicate):
    operation, value = predicate
    if operation == "Eq":
        return case == value
    if operation == "NotEq":
        return case != value
    if operation == "In":
        return case in value
    return case not in value


@lru_cache(maxsize=4)
def _classes(sources):
    try:
        trees = {name: ast.parse(raw, filename=name) for name, raw in sources}
    except SyntaxError as error:
        raise ValueError("unsupported census routing syntax") from error
    constants_by_source = {name: _constants(tree) for name, tree in trees.items()}
    routes = {}
    predicates = []
    for name, tree in trees.items():
        constants = dict(constants_by_source[name])
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module:
                dependency = str(ROOT.joinpath(*statement.module.split(".")).with_suffix(".py"))
                for imported in statement.names:
                    if imported.name in constants_by_source.get(dependency, {}):
                        constants[imported.asname or imported.name] = constants_by_source[dependency][imported.name]
        if Path(name).name.startswith("bus-scale-routine-"):
            for case in constants["CASES"]:
                if case in routes:
                    raise ValueError("overlapping native census adapter routes")
                routes[case] = Path(name).stem
            roots = {"execute_seam"}
        elif Path(name) in _HARNESS_FUNCTIONS:
            roots = _HARNESS_FUNCTIONS[Path(name)]
        else:
            roots = set()
        functions={function.name:function for function in tree.body
                   if isinstance(function,(ast.FunctionDef,ast.AsyncFunctionDef))}
        pending=sorted(roots&functions.keys())
        visited=set()
        while pending:
            function=pending.pop(0)
            if function in visited:
                continue
            visited.add(function)
            try:
                additions,called=_case_predicates(functions[function],constants,functions)
            except ValueError as error:
                raise ValueError(name+":"+str(functions[function].lineno)+" "+function+": "+str(error)) from error
            predicates.extend(additions)
            pending.extend(sorted(called-visited))
    groups = {}
    for case, route in sorted(routes.items()):
        signature = (route, tuple(_matches(case, predicate) for predicate in predicates))
        groups.setdefault(signature, set()).add(case)
    return groups


def execution_path_classes(cases=None):
    paths = [ADAPTER, ENTRY, ROOT / "tests/bus_scale_attention.py", *(FIXTURES / ("bus-scale-routine-" + family + "-2026-10-15.py")
                              for family in ("reads", "writes", "lifecycle", "teardown"))]
    sources = tuple((str(path), path.read_text()) for path in paths)
    classes = _classes(sources)
    known = set().union(*classes.values())
    if cases is not None and set(cases) != known:
        raise ValueError("native census enumeration differs from harness adapter routes")
    return {signature: set(members) for signature, members in classes.items()}


def require_execution_paths(classes, selected):
    selected = set(selected)
    if selected - set().union(*classes.values()):
        raise ValueError("unknown native execution path representative")
    if any(not selected & members for members in classes.values()):
        raise ValueError("uncovered native execution path")


def representative_cases(cases=None):
    classes = execution_path_classes()
    selected = {min(members) for members in classes.values()}
    require_execution_paths(classes, selected)
    return selected if cases is None else selected & set(cases)
