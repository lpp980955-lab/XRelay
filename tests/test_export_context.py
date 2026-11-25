import unittest
from copy import deepcopy
from saxs_waxs_core.export_context import summarize_processing_contexts


class ExportContextTests(unittest.TestCase):
    def test_nested_shared_context_roundtrips_with_duplicate_sample_names(self):
        contexts = [
            {"geometry": {"sdd": 150, "center": [10, 12]}, "source": "a.tif", "optional": None},
            {"geometry": {"sdd": 150, "center": [11, 12]}, "source": "b.tif"},
        ]
        summary = summarize_processing_contexts((i, "same", value) for i, value in enumerate(contexts))
        self.assertEqual(summary['shared'], {"geometry": {"sdd": 150}})
        self.assertEqual([v['curve_index'] for v in summary['per_curve_differences']], [0, 1])

        def merge(shared, delta):
            result = deepcopy(shared)
            for key, value in delta.items():
                result[key] = merge(result[key], value) if isinstance(value, dict) and key in result else value
            return result

        rebuilt = [merge(summary['shared'], entry['context']) for entry in summary['per_curve_differences']]
        self.assertEqual(rebuilt, contexts)
        summary['shared']['geometry']['sdd'] = 200
        self.assertEqual(contexts[0]['geometry']['sdd'], 150)

    def test_empty_and_single_inputs(self):
        self.assertEqual(summarize_processing_contexts([]), {'shared': {}, 'per_curve_differences': []})
        summary = summarize_processing_contexts([(3, 'sample', {'mask': {}})])
        self.assertEqual(summary['shared'], {'mask': {}})
        self.assertEqual(summary['per_curve_differences'][0]['context'], {})
