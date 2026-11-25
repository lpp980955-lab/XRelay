import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from dataclasses import replace
import math
import unittest
import numpy as np
from PySide6 import QtCore,QtWidgets
import pyqtgraph as pg
from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance
from saxs_waxs_core.integration import (IntegrationWorkspace,RadialIntegrationRecipe,
    AzimuthalIntegrationRecipe,AzimuthSector,PixelSplitting,integrate_radial_1d,integrate_azimuthal_1d)
from saxs_waxs_ui.integration_overlay import IntegrationRegionOverlay


def geometry():
    return DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
        pixel_size_x_m=75e-6,pixel_size_y_m=90e-6,beam_center_x_px=40.25,beam_center_y_px=33.75,
        image_width_px=80,image_height_px=64,provenance=GeometryProvenance(method="test"))


class RoiPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_cached_numerics_match_fresh_with_ranges_masks_modes_and_geometry_changes(self):
        values=np.random.default_rng(9).normal(40,15,(64,80))
        original=values.copy()
        mask=np.zeros(values.shape,bool)
        mask[10:15,20:25]=True
        workspace=IntegrationWorkspace()
        initial=geometry()
        for splitting in PixelSplitting:
            recipes=[RadialIntegrationRecipe(npt=70,pixel_splitting=splitting),
                RadialIntegrationRecipe(npt=70,sectors=(AzimuthSector(359,30),),pixel_splitting=splitting),
                AzimuthalIntegrationRecipe(radial_range=(.05,.2),npt_azimuth=72,npt_radial=12,pixel_splitting=splitting),
                RadialIntegrationRecipe(npt=75,sectors=(AzimuthSector(90,35),AzimuthSector(270,35)),pixel_splitting=splitting),
                AzimuthalIntegrationRecipe(radial_range=(.06,.21),npt_azimuth=72,npt_radial=12,pixel_splitting=splitting)]
            for index,recipe in enumerate(recipes):
                current=initial if index<4 else replace(initial,beam_center_x_px=42.125)
                if index==3:mask[:,5]=True
                cached=workspace.integrate(values,current,recipe,mask)
                fresh=(integrate_radial_1d if isinstance(recipe,RadialIntegrationRecipe) else integrate_azimuthal_1d)(values,current,recipe,mask=mask)
                np.testing.assert_array_equal(cached.x,fresh.x)
                np.testing.assert_allclose(cached.intensity,fresh.intensity,rtol=1e-12,atol=1e-12,equal_nan=True)
                np.testing.assert_allclose(cached.count,fresh.count,rtol=1e-12,atol=1e-12)
                self.assertEqual(dict(cached.audit),dict(fresh.audit))
        np.testing.assert_array_equal(values,original)

    def test_analytic_annulus_has_correct_metric_boundaries_without_raster_map(self):
        plot=pg.PlotWidget()
        overlay=IntegrationRegionOverlay(plot.getPlotItem())
        g=geometry()
        try:
            for unit,low,high in (("q_nm^-1",.1,.2),("q_A^-1",.01,.02),
                ("s_nm^-1",.1/(2*math.pi),.2/(2*math.pi)),("2th_deg",.15,.25)):
                descriptor=dict(mode="azimuthal",radial_unit=unit,radial_lower=low,radial_upper=high)
                overlay.set_region(descriptor,shape=(64,80),center_xy=(g.beam_center_x_px,g.beam_center_y_px),geometry=g)
                self.assertIsNone(overlay._radial_map)
                self.assertFalse(any(isinstance(item,pg.IsocurveItem) for item in overlay._contours))
                for index,level in enumerate((low,high)):
                    angle=math.radians(level) if unit=="2th_deg" else 2*math.asin(.1*g.wavelength_m*1e9/(4*math.pi)*(index+1))
                    radius=g.detector_distance_m*math.tan(angle)
                    rect=overlay._contours[index].rect()
                    self.assertAlmostEqual(rect.width()/2,radius/g.pixel_size_x_m,places=9)
                    self.assertAlmostEqual(rect.height()/2,radius/g.pixel_size_y_m,places=9)
                    point=QtCore.QPointF(g.beam_center_x_px+radius/g.pixel_size_x_m,g.beam_center_y_px)
                    self.assertAlmostEqual(overlay._sample_radial_map(point),level,places=10)
                handles=(overlay._lower_handle,overlay._upper_handle)
                updated={**descriptor,"radial_upper":high*1.01}
                overlay.set_region(updated,shape=(64,80),center_xy=(g.beam_center_x_px,g.beam_center_y_px),geometry=g)
                self.assertEqual(handles,(overlay._lower_handle,overlay._upper_handle))
                self.assertAlmostEqual(overlay._sample_radial_map(overlay._upper_handle.pos()),high*1.01,places=10)
                overlay.set_selected(True)
        finally:
            overlay.clear()
            plot.close()

    def test_cached_backend_preserves_corrections_units_and_tilt(self):
        g=replace(geometry(),rot1_rad=.01,rot2_rad=-.005)
        data=np.random.default_rng(17).normal(30,10,(64,80))
        mask=np.zeros(data.shape,bool)
        mask[::7,::5]=True
        workspace=IntegrationWorkspace()
        recipes=(RadialIntegrationRecipe(npt=60,unit="s_nm^-1",correct_solid_angle=True,polarization_factor=.7,normalization_factor=2.3),
            AzimuthalIntegrationRecipe(radial_range=(.1,.5),radial_unit="2th_deg",npt_azimuth=72,npt_radial=12,correct_solid_angle=True,polarization_factor=-.3),
            RadialIntegrationRecipe(npt=65,unit="q_A^-1",sectors=(AzimuthSector(100,40),)),
            AzimuthalIntegrationRecipe(radial_range=(.02,.08),radial_unit="s_nm^-1",npt_azimuth=80,npt_radial=16))
        for recipe in recipes:
            cached=workspace.integrate(data,g,recipe,mask)
            fresh=(integrate_radial_1d if isinstance(recipe,RadialIntegrationRecipe) else integrate_azimuthal_1d)(data,g,recipe,mask=mask)
            np.testing.assert_array_equal(cached.x,fresh.x)
            np.testing.assert_allclose(cached.intensity,fresh.intensity,rtol=1e-12,atol=1e-12,equal_nan=True)
            np.testing.assert_allclose(cached.count,fresh.count,rtol=1e-12,atol=1e-12)
            self.assertEqual(dict(cached.audit),dict(fresh.audit))

    def test_tilted_geometry_keeps_exact_raster_contour_path(self):
        from saxs_waxs_core.integration import detector_radial_array
        g=replace(geometry(),rot1_rad=.01)
        radial=detector_radial_array(g,(64,80))
        plot=pg.PlotWidget()
        overlay=IntegrationRegionOverlay(plot.getPlotItem())
        try:
            overlay.set_region(dict(mode="azimuthal",radial_unit="q_nm^-1",radial_lower=.1,radial_upper=.2),
                shape=(64,80),center_xy=(g.beam_center_x_px,g.beam_center_y_px),radial_map=radial,geometry=g)
            self.assertIs(overlay._radial_map,radial)
            self.assertTrue(all(isinstance(item,pg.IsocurveItem) for item in overlay._contours))
        finally:
            overlay.clear()
            plot.close()

    def test_annulus_handles_follow_zero_bounds_and_crossing_clamp(self):
        g=geometry()
        plot=pg.PlotWidget()
        overlay=IntegrationRegionOverlay(plot.getPlotItem())
        descriptor=dict(mode="azimuthal",radial_unit="q_nm^-1",radial_lower=0.,radial_upper=.2)
        try:
            overlay.set_region(descriptor,shape=(64,80),center_xy=(g.beam_center_x_px,g.beam_center_y_px),geometry=g)
            self.assertAlmostEqual(overlay._sample_radial_map(overlay._lower_handle.pos()),0.,places=10)
            upper=overlay._upper_handle.pos()
            overlay._lower_handle.setPos(g.beam_center_x_px+2*(upper.x()-g.beam_center_x_px),
                g.beam_center_y_px+2*(upper.y()-g.beam_center_y_px))
            self.assertLess(overlay.descriptor["radial_lower"],.2)
            self.assertAlmostEqual(overlay._sample_radial_map(overlay._lower_handle.pos()),overlay.descriptor["radial_lower"],places=10)
            for low in (0.,.1):
                overlay.set_region({**descriptor,"radial_lower":low},shape=(64,80),center_xy=(g.beam_center_x_px,g.beam_center_y_px),geometry=g)
                self.assertAlmostEqual(overlay._sample_radial_map(overlay._lower_handle.pos()),low,places=10)
        finally:
            overlay.clear()
            plot.close()

    def test_unknown_geometry_dimensions_do_not_reuse_another_image_shape(self):
        workspace=IntegrationWorkspace()
        g=replace(geometry(),image_width_px=None,image_height_px=None)
        recipe=RadialIntegrationRecipe(npt=65,sectors=(AzimuthSector(90,35),))
        for shape in ((64,80),(72,96),(64,80)):
            data=np.random.default_rng(5).normal(30,5,shape)
            cached=workspace.integrate(data,g,recipe)
            fresh=integrate_radial_1d(data,g,recipe)
            np.testing.assert_array_equal(cached.intensity,fresh.intensity)
            np.testing.assert_array_equal(cached.count,fresh.count)
