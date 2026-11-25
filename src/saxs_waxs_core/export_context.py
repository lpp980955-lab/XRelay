"""Compact JSON-compatible processing contexts without losing sample identity."""
from copy import deepcopy
import json


def summarize_processing_contexts(entries):
    """Return shared nested settings and indexed per-curve differences.

    Entries are (index, sample name, context). Indices disambiguate identical
    names. A missing field is distinct from a field explicitly set to None.
    """
    entries = list(entries)

    def same(left, right):
        return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)

    def common(mappings):
        shared = {}
        for key, value in mappings[0].items():
            if not all(key in item for item in mappings):
                continue
            values = [item[key] for item in mappings]
            if all(isinstance(item, dict) for item in values):
                nested = common(values)
                if nested or all(not item for item in values):
                    shared[key] = nested
            elif all(same(value, item) for item in values):
                shared[key] = deepcopy(value)
        return shared

    def difference(context, shared):
        result = {}
        for key, value in context.items():
            if key not in shared:
                result[key] = deepcopy(value)
            elif isinstance(value, dict) and isinstance(shared[key], dict):
                nested = difference(value, shared[key])
                if nested:
                    result[key] = nested
        return result

    shared = common([entry[2] for entry in entries]) if entries else {}
    return {"shared": shared, "per_curve_differences": [
        {"curve_index": index, "sample": name, "context": difference(context, shared)}
        for index, name, context in entries]}
