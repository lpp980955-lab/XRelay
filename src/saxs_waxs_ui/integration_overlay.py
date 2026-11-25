"""Interactive detector-space overlays for integration recipes."""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets


def _visible_angle(x: float, y: float, center_x: float, center_y: float) -> float:
    return math.degrees(math.atan2(x - center_x, -(y - center_y))) % 360.0


def _point_at_angle(
    center_x: float, center_y: float, radius: float, angle_deg: float
) -> QtCore.QPointF:
    angle = math.radians(angle_deg)
    return QtCore.QPointF(
        center_x + radius * math.sin(angle),
        center_y - radius * math.cos(angle),
    )


def _ring_radii(geometry,unit,level):
    from .radial_units import convert_radial_values
    angle=float(convert_radial_values([level],unit,"2th_deg",geometry.wavelength_m)[0])
    if not 0<=angle<90:
        return 0.,0.
    radius=geometry.detector_distance_m*math.tan(math.radians(angle))
    return radius/geometry.pixel_size_x_m,radius/geometry.pixel_size_y_m


class _RingEllipse(QtWidgets.QGraphicsEllipseItem):
    """Exact zero-tilt boundary, clipped without raster contour extraction."""
    def __init__(self,geometry,unit,clip):
        super().__init__()
        self.geometry,self.unit,self.clip=geometry,unit,clip

    def setLevel(self,level):
        rx,ry=_ring_radii(self.geometry,self.unit,level)
        self.setRect(self.geometry.beam_center_x_px-rx,self.geometry.beam_center_y_px-ry,2*rx,2*ry)

    def boundingRect(self):
        return super().boundingRect().intersected(self.clip)

    def paint(self,painter,option,widget=None):
        painter.save()
        painter.setClipRect(self.clip)
        super().paint(painter,option,widget)
        painter.restore()


class _DragTarget(pg.TargetItem):
    drag_state_changed=QtCore.Signal(bool)

    def mouseDragEvent(self,event):
        left=event.button()==QtCore.Qt.MouseButton.LeftButton
        if left and event.isStart():self.drag_state_changed.emit(True)
        try:
            super().mouseDragEvent(event)
        finally:
            if left and event.isFinish():self.drag_state_changed.emit(False)


class IntegrationRegionOverlay(QtCore.QObject):
    """Draw and edit a sector or annulus without altering image data."""

    region_changed = QtCore.Signal(object)
    drag_state_changed=QtCore.Signal(bool)

    def __init__(self, plot_item: pg.PlotItem, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.plot_item = plot_item
        self._items: list[QtWidgets.QGraphicsItem] = []
        self._descriptor: dict[str, Any] | None = None
        self._shape: tuple[int, int] | None = None
        self._center = (0.0, 0.0)
        self._radial_map: np.ndarray | None = None
        self._geometry=None
        self._radial_epsilon=1e-12
        self._display_radial_map: np.ndarray | None = None
        self._display_stride = 1
        self._direction_handle: pg.TargetItem | None = None
        self._width_handle: pg.TargetItem | None = None
        self._lower_handle: pg.TargetItem | None = None
        self._upper_handle: pg.TargetItem | None = None
        self._sector_paths: list[QtWidgets.QGraphicsPathItem] = []
        self._band_item: pg.ImageItem | None = None
        self._contours: list[pg.IsocurveItem] = []
        self._updating = False
        self._visible = True
        self._selected = False
        self.dragging=False

    def _drag_state(self,active):
        self.dragging=bool(active)
        self.drag_state_changed.emit(self.dragging)

    @property
    def descriptor(self) -> dict[str, Any] | None:
        return None if self._descriptor is None else dict(self._descriptor)

    @property
    def handle_count(self) -> int:
        return sum(
            item is not None
            for item in (
                self._direction_handle,
                self._width_handle,
                self._lower_handle,
                self._upper_handle,
            )
        )

    @property
    def is_visible(self) -> bool:
        """Return the requested overlay visibility without discarding its ROI."""

        return self._visible

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_visible(self, visible: bool) -> None:
        """Show or hide every integration graphic while retaining its state."""

        self._visible = bool(visible)
        for item in self._items:
            item.setVisible(self._visible)

    def set_selected(self, selected: bool) -> None:
        """Mark the current integration guide as the Delete-key target."""

        self._selected = bool(selected) and bool(self._items)
        for item in self._sector_paths:
            item.setPen(
                pg.mkPen("#FFD166" if self._selected else "#00D4FF", width=3.2 if self._selected else 1.8)
            )
        for index, contour in enumerate(self._contours):
            colors = ("#00D4FF", "#FFB000")
            contour.setPen(
                pg.mkPen("#FFD166" if self._selected else colors[index], width=3.2 if self._selected else 2.0)
            )

    def contains_scene_point(self, scene_position: QtCore.QPointF) -> bool:
        if not self._visible:
            return False
        candidates = [
            item for item in self._items if item is not self._band_item and item.isVisible()
        ]
        for item in reversed(candidates):
            try:
                local = item.mapFromScene(scene_position)
                if item.shape().contains(local) or item.sceneBoundingRect().adjusted(-4, -4, 4, 4).contains(scene_position):
                    return True
            except (AttributeError, RuntimeError, TypeError):
                continue
        return False

    def clear(self) -> None:
        for item in self._items:
            try:
                self.plot_item.removeItem(item)
            except (RuntimeError, ValueError):
                pass
        self._items.clear()
        self._sector_paths.clear()
        self._contours.clear()
        self._direction_handle = None
        self._width_handle = None
        self._lower_handle = None
        self._upper_handle = None
        self._band_item = None
        self._descriptor = None
        self._radial_map = None
        self._geometry=None
        self.dragging=False
        self._display_radial_map = None
        self._display_stride = 1
        self._selected = False

    def set_region(
        self,
        descriptor: Mapping[str, Any],
        *,
        shape: tuple[int, int],
        center_xy: tuple[float, float],
        radial_map: np.ndarray | None = None,
        geometry=None,
    ) -> None:
        next_descriptor = dict(descriptor)
        next_center = (float(center_xy[0]), float(center_xy[1]))
        if self._can_update_in_place(
            next_descriptor,
            shape=shape,
            center_xy=next_center,
            radial_map=radial_map,
            geometry=geometry,
        ):
            self._descriptor = next_descriptor
            self._center = next_center
            self._updating = True
            try:
                if next_descriptor.get("mode") == "radial":
                    self._update_sector_graphics()
                else:
                    self._update_annulus_graphics()
            finally:
                self._updating = False
            return
        self.clear()
        self._geometry=geometry
        self._descriptor = next_descriptor
        self._shape = shape
        self._center = next_center
        self._radial_map = radial_map
        if radial_map is not None:
            self._radial_epsilon=max(1e-12,float(np.nanmax(radial_map))*1e-9)
        mode = self._descriptor.get("mode")
        if mode == "radial":
            self._create_sector()
        elif mode == "azimuthal":
            if geometry is not None and geometry.is_untilted:
                self._create_analytic_annulus()
                return
            if radial_map is None or radial_map.shape != shape:
                raise ValueError("azimuthal overlay requires a matching radial map")
            # The full-resolution map remains the source of truth for handle
            # sampling and integration values.  Only the translucent display
            # overlay is decimated, because rebuilding two full detector-sized
            # contours for every mouse event makes annulus dragging stutter.
            self._display_stride = max(1, math.ceil(max(shape) / 384))
            self._display_radial_map = radial_map[
                :: self._display_stride, :: self._display_stride
            ]
            self._create_annulus()
        else:
            raise ValueError(f"unsupported integration overlay mode: {mode!r}")

    def _can_update_in_place(
        self,
        descriptor: Mapping[str, Any],
        *,
        shape: tuple[int, int],
        center_xy: tuple[float, float],
        radial_map: np.ndarray | None,
        geometry=None,
    ) -> bool:
        """Return whether a descriptor change can preserve the live drag items.

        Recreating a ``TargetItem`` while another pane is mirroring a drag makes
        multi-pane interaction appear frozen.  Preserve the graphics objects
        whenever only their numeric recipe values changed; rebuild only when the
        overlay topology or detector coordinate system actually changed.
        """

        if self._descriptor is None or self._shape != shape or self._center != center_xy:
            return False
        previous_mode = self._descriptor.get("mode")
        next_mode = descriptor.get("mode")
        if previous_mode != next_mode:
            return False
        if next_mode == "radial":
            return self._descriptor.get("sector_mode", "full") == descriptor.get(
                "sector_mode", "full"
            )
        if next_mode == "azimuthal":
            if self._geometry is not None and self._geometry.is_untilted:
                return geometry==self._geometry and descriptor.get("radial_unit")==self._descriptor.get("radial_unit")
            return radial_map is self._radial_map and radial_map is not None
        return False

    def _add(self, item: QtWidgets.QGraphicsItem) -> None:
        self.plot_item.addItem(item,ignoreBounds=True)
        item.setVisible(self._visible)
        self._items.append(item)

    def _create_sector(self) -> None:
        assert self._descriptor is not None and self._shape is not None
        sector_mode = str(self._descriptor.get("sector_mode", "full"))
        if sector_mode == "full":
            return
        for _index in range(2 if sector_mode == "opposite" else 1):
            item = QtWidgets.QGraphicsPathItem()
            item.setPen(pg.mkPen("#00D4FF", width=1.8))
            item.setBrush(pg.mkBrush(0, 212, 255, 35))
            item.setZValue(24)
            self._add(item)
            self._sector_paths.append(item)
        radius = max(20.0, 0.36 * min(self._shape))
        center = float(self._descriptor.get("center_deg", 90.0)) % 360.0
        width = float(self._descriptor.get("width_deg", 35.0))
        self._direction_handle = _DragTarget(
            pos=_point_at_angle(*self._center, radius, center),
            size=14,
            symbol="o",
            pen=pg.mkPen("#FFFFFF", width=2),
            brush=pg.mkBrush("#0078D4"),
            hoverPen=pg.mkPen("#FFD166", width=3),
            movable=True,
        )
        self._width_handle = _DragTarget(
            pos=_point_at_angle(*self._center, radius, center + width / 2.0),
            size=13,
            symbol="t",
            pen=pg.mkPen("#FFFFFF", width=2),
            brush=pg.mkBrush("#FF7A00"),
            hoverPen=pg.mkPen("#FFD166", width=3),
            movable=True,
        )
        for handle in (self._direction_handle, self._width_handle):
            handle.drag_state_changed.connect(self._drag_state)
            handle.setZValue(32)
            self._add(handle)
            handle.sigPositionChanged.connect(self._sector_handle_changed)
            handle.sigPositionChangeFinished.connect(self._sector_handle_changed)
        self._update_sector_graphics()

    @QtCore.Slot(object)
    def _sector_handle_changed(self, source: pg.TargetItem) -> None:
        if self._updating or self._descriptor is None or self._shape is None:
            return
        cx, cy = self._center
        position = source.pos()
        if source is self._direction_handle:
            self._descriptor["center_deg"] = _visible_angle(
                position.x(), position.y(), cx, cy
            )
        elif source is self._width_handle:
            boundary = _visible_angle(position.x(), position.y(), cx, cy)
            center = float(self._descriptor["center_deg"])
            delta = (boundary - center + 180.0) % 360.0 - 180.0
            self._descriptor["width_deg"] = min(360.0, max(0.1, 2.0 * abs(delta)))
        self._update_sector_graphics()
        self.region_changed.emit(dict(self._descriptor))

    def _update_sector_graphics(self) -> None:
        if self._descriptor is None or self._shape is None:
            return
        cx, cy = self._center
        center = float(self._descriptor.get("center_deg", 90.0)) % 360.0
        width = min(360.0, max(0.1, float(self._descriptor.get("width_deg", 35.0))))
        radius = 1.25 * math.hypot(self._shape[0], self._shape[1])
        detector_path = QtGui.QPainterPath()
        detector_path.addRect(
            QtCore.QRectF(
                0.0,
                0.0,
                float(self._shape[1]),
                float(self._shape[0]),
            )
        )
        self._updating = True
        try:
            handle_radius = max(20.0, 0.36 * min(self._shape))
            if self._direction_handle is not None:
                self._direction_handle.setPos(
                    _point_at_angle(cx, cy, handle_radius, center)
                )
            if self._width_handle is not None:
                self._width_handle.setPos(
                    _point_at_angle(cx, cy, handle_radius, center + width / 2.0)
                )
            for index, item in enumerate(self._sector_paths):
                path = QtGui.QPainterPath(QtCore.QPointF(cx, cy))
                local_center = center + 180.0 * index
                for angle in np.linspace(
                    local_center - width / 2.0,
                    local_center + width / 2.0,
                    65,
                ):
                    path.lineTo(_point_at_angle(cx, cy, radius, float(angle)))
                path.closeSubpath()
                # The long construction radius guarantees coverage of every
                # detector corner.  Clip only the display path to the active
                # raster so the guide never occupies the surrounding black
                # ViewBox; the numerical sector recipe remains unchanged.
                item.setPath(path.intersected(detector_path))
        finally:
            self._updating = False

    def _create_analytic_annulus(self):
        clip=QtCore.QRectF(0,0,self._shape[1],self._shape[0])
        self._band_item=QtWidgets.QGraphicsPathItem()
        self._band_item.setPen(pg.mkPen(None))
        self._band_item.setBrush(pg.mkBrush(0,212,255,34))
        self._band_item.setZValue(23)
        self._add(self._band_item)
        for color in ("#00D4FF","#FFB000"):
            contour=_RingEllipse(self._geometry,self._descriptor["radial_unit"],clip)
            contour.setPen(pg.mkPen(color,width=2))
            contour.setZValue(25)
            self._add(contour)
            self._contours.append(contour)
        self._lower_handle=self._make_radial_handle(float(self._descriptor["radial_lower"]),"#00A6C7","o")
        self._upper_handle=self._make_radial_handle(float(self._descriptor["radial_upper"]),"#D97706","s")
        for handle in (self._lower_handle,self._upper_handle):
            handle.sigPositionChanged.connect(self._annulus_handle_changed)
            handle.sigPositionChangeFinished.connect(self._annulus_handle_changed)
        self._update_annulus_graphics()

    def _create_annulus(self) -> None:
        assert (
            self._descriptor is not None
            and self._radial_map is not None
            and self._display_radial_map is not None
            and self._shape is not None
        )
        self._band_item = pg.ImageItem(axisOrder="row-major")
        self._band_item.setZValue(23)
        self._add(self._band_item)
        for color in ("#00D4FF", "#FFB000"):
            contour = pg.IsocurveItem(
                self._display_radial_map,
                level=0.0,
                pen=pg.mkPen(color, width=2),
                axisOrder="row-major",
            )
            contour.setScale(float(self._display_stride))
            contour.setPos(0.5, 0.5)
            contour.setZValue(25)
            self._add(contour)
            self._contours.append(contour)
        lower = float(self._descriptor["radial_lower"])
        upper = float(self._descriptor["radial_upper"])
        self._lower_handle = self._make_radial_handle(lower, "#00A6C7", "o")
        self._upper_handle = self._make_radial_handle(upper, "#D97706", "s")
        for handle in (self._lower_handle, self._upper_handle):
            handle.sigPositionChanged.connect(self._annulus_handle_changed)
            handle.sigPositionChangeFinished.connect(self._annulus_handle_changed)
        self._update_annulus_graphics()

    def _make_radial_handle(self, level: float, color: str, symbol: str) -> pg.TargetItem:
        handle = _DragTarget(
            pos=self._point_for_level(level,preferred_angle=45. if symbol=="o" else 30.),
            size=14,
            symbol=symbol,
            pen=pg.mkPen("#FFFFFF", width=2),
            brush=pg.mkBrush(color),
            hoverPen=pg.mkPen("#FFD166", width=3),
            movable=True,
        )
        handle.setZValue(32)
        handle.drag_state_changed.connect(self._drag_state)
        self._add(handle)
        return handle

    def _point_for_level(self, level: float,preferred_angle=0.) -> QtCore.QPointF:
        if self._geometry is not None and self._geometry.is_untilted:
            rx,ry=_ring_radii(self._geometry,self._descriptor["radial_unit"],level)
            angles=np.linspace(0,2*np.pi,721)
            xx=self._center[0]+rx*np.cos(angles)
            yy=self._center[1]+ry*np.sin(angles)
            inside=(xx>=0)&(xx<=self._shape[1])&(yy>=0)&(yy<=self._shape[0])
            candidates=np.flatnonzero(inside)
            distance=np.abs((angles-math.radians(preferred_angle)+np.pi)%(2*np.pi)-np.pi)
            index=int(candidates[np.argmin(distance[candidates])]) if candidates.size else 0
            return QtCore.QPointF(float(np.clip(xx[index],0,self._shape[1])),float(np.clip(yy[index],0,self._shape[0])))
        assert self._radial_map is not None
        height, width = self._radial_map.shape
        cx, cy = self._center
        row = int(min(max(round(cy - 0.5), 0), height - 1))
        start = int(min(max(math.floor(cx), 0), width - 1))
        values = self._radial_map[row, start:]
        if values.size:
            column = start + int(np.nanargmin(np.abs(values - level)))
        else:
            flat = int(np.nanargmin(np.abs(self._radial_map - level)))
            row, column = np.unravel_index(flat, self._radial_map.shape)
        return QtCore.QPointF(column + 0.5, row + 0.5)

    def _sample_radial_map(self, position: QtCore.QPointF) -> float:
        if self._geometry is not None and self._geometry.is_untilted:
            geometry=self._geometry
            radius=math.hypot((position.x()-self._center[0])*geometry.pixel_size_x_m,
                (position.y()-self._center[1])*geometry.pixel_size_y_m)
            angle=math.degrees(math.atan2(radius,geometry.detector_distance_m))
            from .radial_units import convert_radial_values
            return float(convert_radial_values([angle],"2th_deg",self._descriptor["radial_unit"],geometry.wavelength_m)[0])
        assert self._radial_map is not None
        row = int(min(max(math.floor(position.y()), 0), self._radial_map.shape[0] - 1))
        column = int(min(max(math.floor(position.x()), 0), self._radial_map.shape[1] - 1))
        return float(self._radial_map[row, column])

    @QtCore.Slot(object)
    def _annulus_handle_changed(self, source: pg.TargetItem) -> None:
        if self._updating or self._descriptor is None or (self._radial_map is None and self._geometry is None):
            return
        value = self._sample_radial_map(source.pos())
        lower = float(self._descriptor["radial_lower"])
        upper = float(self._descriptor["radial_upper"])
        epsilon = self._radial_epsilon
        if source is self._lower_handle:
            lower = min(value, upper - epsilon)
        elif source is self._upper_handle:
            upper = max(value, lower + epsilon)
        self._descriptor["radial_lower"] = lower
        self._descriptor["radial_upper"] = upper
        applied=lower if source is self._lower_handle else upper
        self._update_annulus_graphics(moving_handle=source if applied==value else None)
        self.region_changed.emit(dict(self._descriptor))

    def _update_annulus_graphics(self,moving_handle=None) -> None:
        if self._descriptor is not None and self._geometry is not None and self._geometry.is_untilted:
            lower,upper=float(self._descriptor["radial_lower"]),float(self._descriptor["radial_upper"])
            self._contours[0].setLevel(lower)
            self._contours[1].setLevel(upper)
            was_updating=self._updating
            self._updating=True
            try:
                for handle,contour,level,angle in zip((self._lower_handle,self._upper_handle),self._contours,(lower,upper),(45.,30.)):
                    if handle is None or handle is moving_handle:continue
                    rx,ry=contour.rect().width()/2,contour.rect().height()/2
                    dx,dy=handle.pos().x()-self._center[0],handle.pos().y()-self._center[1]
                    norm=math.hypot(dx/rx,dy/ry) if rx>0 and ry>0 else 0
                    if rx==0 or ry==0:
                        handle.setPos(*self._center)
                    elif norm>0:
                        handle.setPos(self._center[0]+dx/norm,self._center[1]+dy/norm)
                    else:
                        handle.setPos(self._point_for_level(level,preferred_angle=angle))
            finally:
                self._updating=was_updating
            outer,inner=QtGui.QPainterPath(),QtGui.QPainterPath()
            outer.addEllipse(self._contours[1].rect())
            inner.addEllipse(self._contours[0].rect())
            clip=QtGui.QPainterPath()
            clip.addRect(QtCore.QRectF(0,0,self._shape[1],self._shape[0]))
            self._band_item.setPath(outer.subtracted(inner).intersected(clip))
            return
        if self._descriptor is None or self._display_radial_map is None:
            return
        lower = float(self._descriptor["radial_lower"])
        upper = float(self._descriptor["radial_upper"])
        selected = (self._display_radial_map >= lower) & (
            self._display_radial_map <= upper
        )
        rgba = np.zeros(selected.shape + (4,), dtype=np.uint8)
        rgba[selected] = (0, 212, 255, 34)
        assert self._band_item is not None
        self._band_item.setImage(rgba, autoLevels=False)
        # setRect must run after setImage: before the first image is installed,
        # ImageItem has no raster dimensions and derives an invalid transform.
        # Reapplying the detector rect here keeps the decimated overlay in the
        # exact full-resolution detector coordinate system.
        assert self._shape is not None
        self._band_item.setRect(
            QtCore.QRectF(0.0, 0.0, float(self._shape[1]), float(self._shape[0]))
        )
        self._contours[0].setLevel(lower)
        self._contours[1].setLevel(upper)
