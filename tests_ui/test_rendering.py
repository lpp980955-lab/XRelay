import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import matplotlib as mpl
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from saxs_waxs_ui.rendering import publication_render_context


class RenderingTests(unittest.TestCase):
    def test_ui_thread_does_not_wait_for_background_render(self):
        entered, release = threading.Event(), threading.Event()
        def render():
            with publication_render_context({}):
                entered.set()
                release.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(render)
            self.assertTrue(entered.wait(timeout=5))
            try:
                with self.assertRaisesRegex(ValueError, "正在导出"):
                    with publication_render_context({}):
                        self.fail("UI must not enter a busy rendering context")
            finally:
                release.set()
            future.result(timeout=5)
        with publication_render_context({}):
            pass

    def test_concurrent_styles_render_and_restore_without_cross_contamination(self):
        baseline = mpl.rcParams['lines.linewidth']
        barrier = threading.Barrier(2)

        def render(width):
            barrier.wait(timeout=5)
            with publication_render_context({'lines.linewidth': width}):
                figure = Figure(figsize=(1, 1))
                FigureCanvasAgg(figure)
                axes = figure.add_subplot(111)
                line, = axes.plot([0, 1], [0, 1])
                with publication_render_context({'lines.linewidth': width + 1}):
                    self.assertEqual(mpl.rcParams['lines.linewidth'], width + 1)
                self.assertEqual(mpl.rcParams['lines.linewidth'], width)
                output = io.BytesIO()
                figure.savefig(output, format='png')
                self.assertTrue(output.getvalue().startswith(b'\x89PNG'))
                return line.get_linewidth()

        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(executor.map(render, [2.0, 4.0]))
        self.assertEqual(values, [2.0, 4.0])
        self.assertEqual(mpl.rcParams['lines.linewidth'], baseline)

    def test_failure_restores_style_and_releases_lock(self):
        baseline = mpl.rcParams['lines.linewidth']
        with self.assertRaisesRegex(RuntimeError, 'render failed'):
            with publication_render_context({'lines.linewidth': 7}):
                raise RuntimeError('render failed')
        with ThreadPoolExecutor(max_workers=1) as executor:
            def after_failure():
                with publication_render_context({}):
                    return mpl.rcParams['lines.linewidth']
            self.assertEqual(executor.submit(after_failure).result(timeout=5), baseline)
