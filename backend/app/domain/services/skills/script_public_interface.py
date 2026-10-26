"""Read-only syntax projection of CLI and output structure, never computations.

The independent oracle sees names, paths and container shapes. It cannot see
source code, expressions, data values, calculated results or numerical answers.
Unknown/dynamic structure stays unknown instead of becoming invented schema.
"""
from __future__ import annotations

import ast
from copy import deepcopy

_UNKNOWN = {"type": "unknown"}


def public_script_interface(content: str) -> dict:
    tree = ast.parse(content)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    column_sequences = set()

    def scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return tree

    def nodes_in(owner):
        return [node for node in ast.walk(owner) if node is owner or scope(node) is owner]

    def blocks(item):
        result = set()
        while item in parents:
            parent = parents[item]
            result.update((id(parent), field) for field, values in ast.iter_fields(parent)
                          if isinstance(values, list) and item in values)
            item = parent
        return result

    def assignment(name, at):
        found = []
        for node in nodes_in(scope(at)):
            if isinstance(node, ast.Assign) and node.lineno < at.lineno:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        found.append((node.lineno, node.value, None))
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for index, item in enumerate(target.elts):
                            if isinstance(item, ast.Name) and item.id == name:
                                found.append((node.lineno, node.value, index))
        if not found:
            return None
        latest = max(found, key=lambda item: item[0])
        owner = scope(at)
        candidate = next((node for node in nodes_in(owner) if isinstance(node, ast.Assign)
                          and node.lineno == latest[0]), None)
        # A loop-local assignment dominates later uses inside that same loop,
        # but a branch-local assignment does not bind uses after the branch.
        if candidate is None or not blocks(candidate) <= blocks(at):
            return None
        return latest

    def merge(left, right):
        if left == right:
            return deepcopy(left)
        if left.get("type") == right.get("type") == "object":
            a, b = left["properties"], right["properties"]
            return {"type": "object", "properties": {key: merge(a[key], b[key]) if key in a and key in b
                    else dict(_UNKNOWN) if (key not in a and left.get("additional_keys", True)
                                            or key not in b and right.get("additional_keys", True))
                    else deepcopy(a.get(key, b.get(key))) for key in a.keys() | b.keys()},
                    "additional_keys": left.get("additional_keys", True) or right.get("additional_keys", True)}
        if left.get("type") == right.get("type") == "array":
            result = {"type": "array", "items": merge(left["items"], right["items"])}
            if left.get("column_name_prefix") and left.get("column_name_prefix") == right.get("column_name_prefix"):
                result["column_name_prefix"] = list(left["column_name_prefix"])
            return result
        return dict(_UNKNOWN)

    def key_name(node, at, seen=frozenset()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id not in seen:
            binding = assignment(node.id, at)
            if binding and binding[2] is None:
                return key_name(binding[1], binding[1], seen | {node.id})
            declarations = [item.value for item in tree.body if isinstance(item, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == node.id for target in item.targets)]
            if len(declarations) == 1:
                return key_name(declarations[0], declarations[0], seen | {node.id})
        return None

    def update_object(result, incoming):
        if result.get("type") != "object":
            return dict(_UNKNOWN)
        result = deepcopy(result)
        if incoming.get("type") != "object":
            result["properties"] = {key: dict(_UNKNOWN) for key in result["properties"]}
            result["additional_keys"] = True
        else:
            if incoming.get("additional_keys", True):
                result["properties"] = {key: dict(_UNKNOWN) for key in result["properties"]}
            result["properties"].update(incoming["properties"])
            result["additional_keys"] |= incoming.get("additional_keys", True)
        return result

    def after_mutations(name, start, at, result, seen):
        appended = []
        for node in sorted(nodes_in(scope(at)), key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0))):
            if not start < getattr(node, "lineno", 0) < at.lineno:
                continue
            previous = deepcopy(result)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for target in targets:
                root = target
                while isinstance(root, (ast.Subscript, ast.Attribute)):
                    root = root.value
                if target is not root and isinstance(root, ast.Name) and root.id == name:
                    if (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Subscript)
                            and target.value is root and result.get("type") == "object"):
                        key = key_name(target.slice, node)
                        incoming = {"type": "object", "properties": {key: shape(node.value, node, seen)} if key is not None else {},
                                    "additional_keys": key is None}
                        result = update_object(result, incoming)
                    else:
                        return dict(_UNKNOWN)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id == name:
                # Tracking mutations through aliases would need full data flow.
                return dict(_UNKNOWN)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == name):
                method = node.func.attr
                if method == "append" and node.args and result.get("type") == "array":
                    value = shape(node.args[0], node, seen)
                    if isinstance(node.args[0], ast.Name):
                        # Appending a dictionary stores the object, not a
                        # snapshot; later writes through that same local name
                        # can still contribute fields to the serialized item.
                        value = after_mutations(node.args[0].id, node.lineno, at, value, seen)
                    appended.append(value)
                elif method == "update" and result.get("type") == "object":
                    for argument in node.args:
                        result = update_object(result, shape(argument, node, seen))
                    for keyword in node.keywords:
                        result = update_object(result, shape(keyword.value, node, seen) if keyword.arg is None else {
                            "type": "object", "properties": {keyword.arg: shape(keyword.value, node, seen)}, "additional_keys": False})
                elif method not in {"get", "keys", "values", "items", "copy"}:
                    return dict(_UNKNOWN)
            if result != previous and not blocks(node) <= blocks(at):
                result = merge(previous, result)
        if appended and result.get("type") == "array":
            # Empty arrays carry no element evidence. Appended values do;
            # merge only structural possibilities, never data or expressions.
            values = ([result["items"]] if result["items"] != _UNKNOWN else []) + appended
            result["items"] = values[0]
            for value in values[1:]:
                result["items"] = merge(result["items"], value)
        return result

    def shape(node, at=None, seen=frozenset()):
        if node is None or id(node) in seen:
            return dict(_UNKNOWN)
        seen = seen | {id(node)}
        at = at or node
        sequence = column_sequence(node, at)
        if sequence in column_sequences:
            prefix = []
            for kind, value in sequence:
                if kind != "name":
                    break
                prefix.append(value)
            if prefix:
                return {"type": "array", "items": dict(_UNKNOWN), "column_name_prefix": prefix}
        if isinstance(node, ast.BinOp):
            serialized = json_text_payload(node, at)
            if serialized is not None:
                payload, serialization = serialized
                return shape(payload, serialization, seen)
        if isinstance(node, ast.Dict):
            result = {"type": "object", "properties": {}, "additional_keys": False}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    result = update_object(result, shape(value, node, seen))
                else:
                    name = key_name(key, node)
                    result = update_object(result, {"type": "object", "properties": {
                        name: shape(value, node, seen)} if name is not None else {}, "additional_keys": name is None})
            return result
        if isinstance(node, ast.Tuple):
            return {"type": "tuple", "items": [shape(item, node, seen) for item in node.elts]}
        if isinstance(node, ast.List):
            values = [shape(item, node, seen) for item in node.elts]
            result = values[0] if values else dict(_UNKNOWN)
            for value in values[1:]:
                result = merge(result, value)
            return {"type": "array", "items": result}
        if isinstance(node, ast.Name):
            binding = assignment(node.id, at)
            if binding is None:
                return dict(_UNKNOWN)
            _, value, index = binding
            result = shape(value, value, seen)
            if index is not None:
                return (result["items"][index] if result.get("type") == "tuple" and index < len(result["items"])
                        else dict(_UNKNOWN))
            return after_mutations(node.id, binding[0], at, result, seen)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in functions:
                returns = [item for item in nodes_in(functions[node.func.id]) if isinstance(item, ast.Return)]
                values = [shape(item.value, item, seen) for item in returns]
                return values[0] if values and all(item == values[0] for item in values) else dict(_UNKNOWN)
            if isinstance(node.func, ast.Name) and node.func.id == "dict":
                result = {"type": "object", "properties": {}, "additional_keys": False}
                for argument in node.args:
                    result = update_object(result, shape(argument, node, seen))
                for keyword in node.keywords:
                    result = update_object(result, shape(keyword.value, node, seen) if keyword.arg is None else {
                        "type": "object", "properties": {keyword.arg: shape(keyword.value, node, seen)}, "additional_keys": False})
                return result
            if isinstance(node.func, ast.Attribute) and node.func.attr == "dumps" and node.args:
                return shape(node.args[0], node, seen)
        # All scalar expressions/values are deliberately erased.
        return dict(_UNKNOWN)

    def json_text_payload(node, at=None, seen=frozenset(), depth=0):
        """Locate one proven JSON serialization, allowing JSON whitespace only.

        No text is executed, concatenated or exposed. A dynamic suffix, a second
        JSON document or any other string operation stays unknown. Bound both
        expression traversal and alias recursion independently of source size.
        """
        if node is None or id(node) in seen or depth >= 64:
            return None
        seen = seen | {id(node)}
        at = at or node
        if isinstance(node, ast.Name):
            binding = assignment(node.id, at)
            if binding and binding[2] is None and not mutated(node.id, binding[0], at):
                return json_text_payload(binding[1], binding[1], seen, depth + 1)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            def whitespace(value):
                return isinstance(value, ast.Constant) and isinstance(value.value, str) and not set(value.value) - set(" \t\r\n")
            if whitespace(node.left):
                return json_text_payload(node.right, at, seen, depth + 1)
            if whitespace(node.right):
                return json_text_payload(node.left, at, seen, depth + 1)
            return None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dumps" and isinstance(node.func.value, ast.Name)
                and node.func.value.id in json_modules
                and not any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                            and item.name == node.func.value.id for item in ast.walk(tree))):
            if (len(node.args) > 1 or any(isinstance(item, ast.Starred) for item in node.args)
                    or any(item.arg is None or item.arg in {"cls", "default"} and not (
                        isinstance(item.value, ast.Constant) and item.value.value is None) for item in node.keywords)
                    or any(isinstance(item, ast.Attribute) and isinstance(item.ctx, (ast.Store, ast.Del))
                           and isinstance(item.value, ast.Name) and item.value.id == node.func.value.id
                           and item.attr == "dumps" for item in ast.walk(tree))):
                return None
            payload = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg == "obj"), None)
            if payload is not None:
                return payload, node
        return None

    filename_options = {}
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"):
            continue
        flags = [item.value for item in call.args if isinstance(item, ast.Constant)
                 and isinstance(item.value, str) and item.value.startswith("--")]
        keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
        default = keywords.get("default")
        if flags and isinstance(default, ast.Constant) and isinstance(default.value, str):
            value = default.value
            if value and value not in {".", ".."} and "/" not in value and "\\" not in value:
                dest = keywords.get("dest")
                attribute = dest.value if isinstance(dest, ast.Constant) and isinstance(dest.value, str) else flags[0][2:].replace("-", "_")
                filename_options[attribute] = (value, flags[0])

    def filename(node, at=None, seen=frozenset()):
        if node is None or id(node) in seen:
            return None
        seen = seen | {id(node)}
        at = at or node
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value, None) if node.value and "/" not in node.value and "\\" not in node.value else None
        if isinstance(node, ast.Attribute) and node.attr in filename_options:
            return filename_options[node.attr]
        if isinstance(node, ast.Name):
            binding = assignment(node.id, at)
            if binding and binding[2] is None:
                return filename(binding[1], binding[1], seen)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            function = functions[node.func.id]
            returns = [item.value for item in nodes_in(function) if isinstance(item, ast.Return)]
            # Validation-only wrappers can return the very same argument.
            # Never execute or infer transforms from expressions in the body.
            if returns and all(isinstance(item, ast.Name) and item.id == returns[0].id for item in returns):
                parameters = [item.arg for item in function.args.args]
                rebound = any(isinstance(target, ast.Name) and target.id == returns[0].id
                    for statement in nodes_in(function)
                    for target in (statement.targets if isinstance(statement, ast.Assign)
                                   else [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign)) else []))
                if returns[0].id in parameters and not rebound:
                    index = parameters.index(returns[0].id)
                    argument = node.args[index] if index < len(node.args) else next(
                        (item.value for item in node.keywords if item.arg == returns[0].id), None)
                    return filename(argument, node, seen)
        return None

    path_options = {}

    def destination(node, at=None, seen=frozenset()):
        if node is None or id(node) in seen:
            return None
        seen = seen | {id(node)}
        at = at or node
        if isinstance(node, ast.Attribute) and node.attr == "output":
            return "@output"
        if isinstance(node, ast.Name):
            bindings = [item.context_expr for owner in ast.walk(tree) if isinstance(owner, ast.With)
                and owner.lineno < at.lineno <= getattr(owner, "end_lineno", owner.lineno)
                for item in owner.items if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == node.id]
            if bindings:
                return destination(bindings[-1], bindings[-1], seen)
            binding = assignment(node.id, at)
            return destination(binding[1], binding[1], seen) if binding and binding[2] is None else None
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"Path", "str", "open"} and node.args:
                return destination(node.args[0], node, seen)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "absolute", "open"}:
                return destination(node.func.value, node, seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            prefix = destination(node.left, node, seen)
            suffix = filename(node.right, node, seen)
            if prefix and suffix:
                result = prefix + "/" + suffix[0]
                if suffix[1]:
                    path_options[result] = suffix[1]
                return result
        return None

    def mutated(name, start, at):
        aliases = {name}
        for _ in nodes_in(scope(at)):
            before = len(aliases)
            for node in nodes_in(scope(at)):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                    names = {node.value.id, *(target.id for target in node.targets if isinstance(target, ast.Name))}
                    if names & aliases:
                        aliases.update(names)
            if len(aliases) == before:
                break
        for node in nodes_in(scope(at)):
            if not start < getattr(node, "lineno", 0) < at.lineno:
                continue
            if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.Assign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for target in targets:
                    while isinstance(target, (ast.Subscript, ast.Attribute)):
                        target = target.value
                    if isinstance(target, ast.Name) and target.id in aliases and not (
                            isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
                            and node.value.id in aliases and all(isinstance(item, ast.Name) for item in targets)):
                        return True
            if isinstance(node, ast.Call):
                def receiver(value):
                    while isinstance(value, (ast.Subscript, ast.Attribute)):
                        value = value.value
                    return value.id if isinstance(value, ast.Name) else None
                if (isinstance(node.func, ast.Attribute) and receiver(node.func.value) in aliases
                        and node.func.attr not in {"count", "index"}):
                    return True
                passed = any(receiver(value) in aliases for value in [*node.args, *(item.value for item in node.keywords)])
                if passed:
                    pure_builtin = (isinstance(node.func, ast.Name) and node.func.id in {"len", "tuple", "list", "sorted"}
                                    and import_is_unshadowed(node.func.id))
                    json_read = (isinstance(node.func, ast.Attribute) and node.func.attr in {"dump", "dumps"}
                                 and isinstance(node.func.value, ast.Name) and node.func.value.id in json_modules)
                    if not pure_builtin and not json_read:
                        return True
        return False

    global_stability = {}

    def stable_global(name, declaration):
        """Mutable global column lists need a whole-source no-mutation proof.

        Lexical line ordering inside one function cannot establish a global
        object's state: a helper or module statement can mutate it before a
        writer runs. Follow visible aliases/returned containers conservatively;
        unknown calls receiving the object cannot certify a fixed interface.
        """
        if name in global_stability:
            return global_stability[name]
        owners = [tree, *functions.values()]
        global_names = {id(owner): {item for node in nodes_in(owner) if isinstance(node, ast.Global)
                                   for item in node.names} for owner in owners}
        local_names = {id(owner): {node.id for node in nodes_in(owner)
                                  if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
                       | {node.arg for node in nodes_in(owner) if isinstance(node, ast.arg)} for owner in owners}

        def identity(node):
            owner = scope(node)
            if owner is tree or node.id in global_names.get(id(owner), set()) or node.id not in local_names.get(id(owner), set()):
                return (id(tree), node.id)
            return (id(owner), node.id)

        root_key = (id(tree), name)
        aliases = {root_key, *(identity(target) for target in declaration.targets if isinstance(target, ast.Name))}
        returned = set()

        def carries_alias(node):
            if isinstance(node, ast.Name):
                return identity(node) in aliases
            if isinstance(node, (ast.Subscript, ast.Attribute)):
                return carries_alias(node.value)
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return any(carries_alias(item) for item in node.elts)
            if isinstance(node, ast.Dict):
                return any(carries_alias(item) for item in node.values)
            return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in returned

        nodes = list(ast.walk(tree))
        while True:
            before = (len(aliases), len(returned))
            for node in nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and carries_alias(node.value):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    aliases.update(identity(target) for target in targets if isinstance(target, ast.Name))
                elif isinstance(node, ast.Return) and carries_alias(node.value):
                    owner = scope(node)
                    if isinstance(owner, ast.FunctionDef):
                        returned.add(owner.name)
            if before == (len(aliases), len(returned)):
                break
        # An immutable tuple of string column names cannot be changed by an
        # ordinary callee. Rebinding its global name still invalidates proof.
        immutable = isinstance(declaration.value, ast.Tuple) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in declaration.value.elts)
        stable = True
        for node in nodes:
            if node is declaration:
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        if identity(target) == root_key or (not immutable and isinstance(node, ast.AugAssign) and identity(target) in aliases):
                            stable = False
                    elif not immutable and isinstance(target, (ast.Subscript, ast.Attribute)) and carries_alias(target):
                        stable = False
            if not immutable and isinstance(node, ast.Call):
                receiver = isinstance(node.func, ast.Attribute) and carries_alias(node.func.value)
                if receiver and node.func.attr not in {"count", "index", "get", "keys", "values", "items", "copy"}:
                    stable = False
                if any(carries_alias(value) for value in [*node.args, *(item.value for item in node.keywords)]):
                    pure = isinstance(node.func, ast.Name) and node.func.id in {"len", "tuple", "list", "sorted"} and import_is_unshadowed(node.func.id)
                    json_read = isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in json_modules and node.func.attr in {"dump", "dumps"}
                    csv_fields = (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                                  and node.func.value.id in csv_modules and node.func.attr == "DictWriter"
                                  or isinstance(node.func, ast.Name) and csv_imports.get(node.func.id) == "DictWriter")
                    csv_write = False
                    if (isinstance(node.func, ast.Attribute) and node.func.attr in {"writerow", "writerows"}
                            and isinstance(node.func.value, ast.Name)):
                        binding = assignment(node.func.value.id, node)
                        constructor = binding[1] if binding and binding[2] is None else None
                        if isinstance(constructor, ast.Call):
                            api = constructor.func
                            csv_write = (isinstance(api, ast.Attribute) and isinstance(api.value, ast.Name)
                                and api.value.id in csv_modules and api.attr in {"writer", "DictWriter"}
                                or isinstance(api, ast.Name) and api.id in csv_imports)
                    if not (pure or json_read or csv_fields or csv_write or receiver and node.func.attr in {"count", "index"}):
                        stable = False
        global_stability[name] = stable
        return stable

    def column_sequence(node, at=None, seen=frozenset(), index=None, members=()):
        """Internal symbolic identity, never serialized expressions or values.

        Only literal string names may be exposed, and only after the same
        sequence is proven to declare a CSV header below. Dynamic tails remain
        opaque AST identities; no iteration, arithmetic or evaluation occurs.
        """
        if node is None or (id(node), index, members) in seen:
            return None
        seen = seen | {(id(node), index, members)}
        at = at or node
        if isinstance(node, ast.Subscript):
            key = key_name(node.slice, node)
            if key is None:
                return None
            return column_sequence(node.value, node, seen, index, (key, *members))
        if isinstance(node, ast.Name):
            binding = assignment(node.id, at)
            if binding:
                if mutated(node.id, binding[0], at):
                    return None
                if scope(binding[1]) is tree:
                    declaration = parents.get(binding[1])
                    if isinstance(declaration, ast.Assign) and not stable_global(node.id, declaration):
                        return None
                selected = binding[2] if index is None else index
                return column_sequence(binding[1], binding[1], seen, selected, members)
            globals_ = [item for item in tree.body if isinstance(item, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == node.id for target in item.targets)]
            if len(globals_) == 1:
                if not stable_global(node.id, globals_[0]):
                    return None
                return column_sequence(globals_[0].value, globals_[0].value, seen, index, members)
            return (("dynamic", (id(scope(node)), node.id)),) if index is None and not members else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            returns = [item for item in nodes_in(functions[node.func.id]) if isinstance(item, ast.Return)]
            values = [column_sequence(item.value, item, seen, index, members) for item in returns]
            return values[0] if values and values[0] is not None and all(item == values[0] for item in values) else None
        if index is not None:
            if isinstance(node, (ast.Tuple, ast.List)) and index < len(node.elts):
                return column_sequence(node.elts[index], node, seen, members=members)
            return None
        if members:
            if not isinstance(node, ast.Dict):
                return None
            keys = [key_name(key, node) if key is not None else None for key in node.keys]
            # Unknown keys or spreads could replace the selected member.
            # Reject ambiguity instead of inferring from a matching label.
            if None in keys or len(keys) != len(set(keys)) or members[0] not in keys:
                return None
            return column_sequence(node.values[keys.index(members[0])], node, seen, members=members[1:])
        if isinstance(node, (ast.List, ast.Tuple)):
            result = ()
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    segment = column_sequence(item.value, node, seen)
                    if segment is None:
                        return None
                    result += segment
                else:
                    name = key_name(item, node)
                    if name is None:
                        return None
                    result += (("name", name),)
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = column_sequence(node.left, node, seen), column_sequence(node.right, node, seen)
            return left + right if left is not None and right is not None else None
        return (("dynamic", id(node)),)

    def selected_columns(node, at, row_name):
        """Prove writerow values iterate the very same mapping-key sequence."""
        def reads_key(value, key):
            reads = [item for item in ast.walk(value) if isinstance(item, ast.Subscript)
                     and isinstance(item.value, ast.Name) and item.value.id == row_name]
            return bool(reads) and all(ast.dump(item.slice) == ast.dump(key) for item in reads)
        if isinstance(node, (ast.List, ast.Tuple)):
            result = ()
            for value in node.elts:
                reads = [item for item in ast.walk(value) if isinstance(item, ast.Subscript)
                         and isinstance(item.value, ast.Name) and item.value.id == row_name]
                if not reads or not reads_key(value, reads[0].slice):
                    return None
                name = key_name(reads[0].slice, at)
                if name is None:
                    return None
                result += (("name", name),)
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = selected_columns(node.left, at, row_name), selected_columns(node.right, at, row_name)
            return left + right if left is not None and right is not None else None
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)) and len(node.generators) == 1:
            generator = node.generators[0]
            if (not generator.ifs and not generator.is_async and isinstance(generator.target, ast.Name)
                    and reads_key(node.elt, ast.Name(id=generator.target.id, ctx=ast.Load()))):
                return column_sequence(generator.iter, at)
        return None

    csv_modules = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                   for alias in node.names if alias.name == "csv"}
    csv_imports = {alias.asname or alias.name: alias.name for node in tree.body
                   if isinstance(node, ast.ImportFrom) and node.module == "csv"
                   for alias in node.names if alias.name in {"writer", "DictWriter"}}
    def import_is_unshadowed(name):
        return not any(isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
                       or isinstance(node, ast.arg) and node.arg == name for node in ast.walk(tree))
    csv_modules = {name for name in csv_modules if import_is_unshadowed(name)}
    csv_imports = {name: value for name, value in csv_imports.items() if import_is_unshadowed(name)}
    json_modules = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                    for alias in node.names if alias.name == "json" and import_is_unshadowed(alias.asname or alias.name)}
    for declaration in ast.walk(tree):
        if not (isinstance(declaration, ast.Assign) and len(declaration.targets) == 1
                and isinstance(declaration.targets[0], ast.Name) and isinstance(declaration.value, ast.Call)):
            continue
        constructor = declaration.value
        if (isinstance(constructor.func, ast.Attribute) and isinstance(constructor.func.value, ast.Name)
                and constructor.func.value.id in csv_modules):
            api = constructor.func.attr
        elif isinstance(constructor.func, ast.Name):
            api = csv_imports.get(constructor.func.id)
        else:
            api = None
        if api not in {"writer", "DictWriter"} or not constructor.args or not destination(constructor.args[0], constructor):
            continue
        writer = declaration.targets[0].id
        calls = sorted([node for node in nodes_in(scope(declaration)) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == writer and node.lineno > declaration.lineno], key=lambda node: (node.lineno, node.col_offset))
        if not calls or blocks(calls[0]) != blocks(declaration):
            continue
        if any(assignment(writer, call) != (declaration.lineno, constructor, None) for call in calls):
            continue
        if any(isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
               and any(isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                       and target.value.id == writer
                       for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))
               for node in nodes_in(scope(declaration))):
            continue
        if api == "DictWriter":
            fields = constructor.args[1] if len(constructor.args) > 1 else next(
                (item.value for item in constructor.keywords if item.arg == "fieldnames"), None)
            if calls[0].func.attr != "writeheader":
                continue
            sequence = column_sequence(fields, constructor)
        else:
            header = calls[0]
            if header.func.attr != "writerow" or not header.args or len(calls) < 2:
                continue
            sequence = column_sequence(header.args[0], header)
            for call in calls[1:]:
                owner = parents.get(call)
                while owner is not None and not isinstance(owner, (ast.For, ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents.get(owner)
                if (call.func.attr != "writerow" or not call.args or not isinstance(owner, ast.For)
                        or not isinstance(owner.target, ast.Name)
                        or selected_columns(call.args[0], call, owner.target.id) != sequence):
                    sequence = None
                    break
        if sequence and any(item[0] == "name" for item in sequence):
            column_sequences.add(sequence)

    cli = []
    json_outputs = []
    modes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name == "add_argument":
            flags = [item.value for item in node.args if isinstance(item, ast.Constant)
                     and isinstance(item.value, str) and item.value.startswith("--")]
            if not flags:
                continue
            info = {"flags": flags}
            for keyword in node.keywords:
                # Free-form help can contain implementation-specific answers or
                # instructions. Method semantics come from the frozen sources,
                # never from explanatory strings inside the tested program.
                if keyword.arg in {"required", "nargs", "action"} and isinstance(keyword.value, ast.Constant):
                    info[keyword.arg] = keyword.value.value
                elif keyword.arg == "type" and isinstance(keyword.value, ast.Name) and keyword.value.id in {"str", "int", "float"}:
                    info["value_type"] = keyword.value.id
                elif keyword.arg == "choices" and isinstance(keyword.value, (ast.List, ast.Tuple)) and all(isinstance(item, ast.Constant) for item in keyword.value.elts):
                    info["choices"] = [item.value for item in keyword.value.elts]
            cli.append(info)
        payload, target = None, None
        if name == "dump":
            payload = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg == "obj"), None)
            target = node.args[1] if len(node.args) > 1 else next((item.value for item in node.keywords if item.arg == "fp"), None)
        elif name == "write_text" and node.args:
            target, payload = node.func.value, node.args[0]
        elif name in {"to_json", "to_csv", "to_parquet", "to_excel"}:
            target = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg in {"path_or_buf", "path", "excel_writer"}), None)
        resolved = destination(target, node)
        if resolved:
            modes.add("file" if resolved == "@output" else "directory")
            result = shape(payload, node)
            if result.get("type") in {"object", "array"}:
                json_outputs.append({"path": resolved, "shape": result,
                                     **({"filename_argument": path_options[resolved]} if resolved in path_options else {})})
    return {"cli_options": cli, "output_mode": next(iter(modes)) if len(modes) == 1 else "unknown",
            "json_outputs": json_outputs}
