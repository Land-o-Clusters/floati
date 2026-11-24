"""Lossless native-discovery inventory; never execute or deduplicate methods."""

from collections import Counter, defaultdict
import unittest


def _leaves(suite):
    for child in suite:
        if isinstance(child, unittest.TestSuite):
            yield from _leaves(child)
        else:
            yield child


class _OriginLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.origins = {}

    def loadTestsFromModule(self, module, *args, **kwargs):
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        for test in _leaves(suite):
            self.origins[id(test)] = module.__name__
        return suite


def native_census():
    """Inventory canonical root discovery, retaining every occurrence in order."""
    loader = _OriginLoader()
    suite = loader.discover(start_dir=".", top_level_dir=".")
    if loader.errors:
        raise RuntimeError("\n".join(loader.errors))
    occurrences = []
    origins_by_class = defaultdict(set)
    for test in _leaves(suite):
        origin = loader.origins[id(test)]
        class_name = unittest.util.strclass(type(test))
        occurrences.append({"origin": origin, "class": class_name, "id": test.id()})
        origins_by_class[class_name].add(origin)
    counts = Counter(row["id"] for row in occurrences)
    return {
        "occurrence_count": len(occurrences),
        "distinct_id_count": len(counts),
        "repeated_id_count": sum(count > 1 for count in counts.values()),
        "extra_occurrence_count": sum(count - 1 for count in counts.values()),
        "classes_with_multiple_origins": {
            name: sorted(origins) for name, origins in sorted(origins_by_class.items())
            if len(origins) > 1
        },
        "occurrences": occurrences,
    }
