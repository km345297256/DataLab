"""Choose the smallest review repair with proven host-owned target identity.

Localization never overrides a negative verdict. It authorizes source changes
only; the entire package and a fresh independent review still have to pass.
Unknown, incomplete or design-level localization keeps the workflow route.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ImplementationRevisionScope:
    script_paths: tuple[str, ...]
    requirement_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    localized: bool = False

    def payload(self):
        return {"script_paths": list(self.script_paths),
                "requirement_ids": list(self.requirement_ids), "source_ids": list(self.source_ids)}


def implementation_revision_scope(recipe, review, contract):
    """Fail closed on scope identity without interpreting explanatory prose."""
    scripts = {script.path for script in recipe.scripts}
    if not scripts:
        return None
    excluded = {item.source_id for item in contract.excluded_sources}
    negative_requirements = {item.requirement_id for item in review.requirements if item.status != "preserved"}
    negative_sources = {item.source_id for item in review.sources
                        if item.status != ("valid_exclusion" if item.source_id in excluded else "preserved")}
    scope = review.repair_scope
    if scope is None:
        # Compatibility with already saved reviews. Their old implementation
        # rejection authorized a source transaction across existing scripts.
        if not review.implementation_appropriate and not negative_requirements and not negative_sources:
            return ImplementationRevisionScope(tuple(script.path for script in recipe.scripts))
        return None
    if scope.kind != "implementation":
        return None
    if any(item.status == "unsupported" for item in review.requirements):
        # An unsupported obligation challenges the contract's source basis,
        # which changing an implementation cannot repair.
        return None
    required, sources, selected = set(scope.requirement_ids), set(scope.source_ids), set(scope.script_paths)
    if (len(required) != len(scope.requirement_ids) or len(sources) != len(scope.source_ids)
            or len(selected) != len(scope.script_paths) or not selected or not selected <= scripts):
        return None
    requirements = {item.id: item for item in contract.requirements}
    if (not required <= requirements.keys() or not negative_requirements <= required
            or sources != negative_sources or sources & excluded):
        return None
    # A disagreement with source inclusion/exclusion changes the design. It
    # cannot be relabelled as a code defect even by a localized response.
    if any(item.source_id in sources and item.status not in {"missing", "changed"} for item in review.sources):
        return None
    # A source-level rejection is broader than a named requirement. Require
    # every retained obligation from that source to be included in the repair.
    for source in sources:
        obligations = {item.id for item in contract.requirements if source in item.source_ids}
        if not obligations or not obligations <= required:
            return None
    declarations = {item.path: item for item in recipe.implementation.scripts}
    if not selected <= declarations.keys():
        return None
    coverage = {path: set() for path in selected}
    for binding in recipe.coverage:
        for pointer in binding.locations:
            match = re.fullmatch(r"/scripts/(\d+)(?:/content)?", pointer)
            if match and int(match[1]) < len(recipe.scripts):
                path = recipe.scripts[int(match[1])].path
                if path in coverage:
                    coverage[path].add(binding.requirement_id)
    assigned = {path: set(declarations[path].requirement_ids) & coverage[path] for path in selected}
    if required and (not required <= set().union(*assigned.values())
                     or any(not required & identifiers for identifiers in assigned.values())):
        return None
    # An overall implementation defect may concern execution rather than a
    # method requirement. Empty requirements are allowed only for that case.
    if not required and (review.implementation_appropriate or negative_requirements or sources):
        return None
    return ImplementationRevisionScope(
        tuple(script.path for script in recipe.scripts if script.path in selected),
        tuple(item.id for item in contract.requirements if item.id in required),
        tuple(item.source_id for item in review.sources if item.source_id in sources), True)
