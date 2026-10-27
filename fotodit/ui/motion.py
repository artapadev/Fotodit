"""Springs: the motion of the interface (hover, press, sliding selections, panels appearing).

A spring has no fixed duration. It starts from wherever the value is now, keeps its
velocity when the target changes halfway (so a hover that ends before the highlight has
finished growing turns around smoothly), and settles on its own. It is described the way
Apple describes it: the response (roughly how many seconds it takes to get there) and the
damping ratio (1.0: no overshoot; below 1.0: a little bounce).

All springs share one timer that runs only while something moves. When Windows' animations
are turned off (Settings → Accessibility → Visual effects → Animation effects), springs jump
straight to their target.
"""

from __future__ import annotations

import math
import sys
import time

import shiboken6
from PySide6.QtCore import QObject, QTimer, Signal

_FRAME_MS = 16
_EPS = 1e-3


def animations_enabled() -> bool:
    """Windows' "Animation effects" (SPI_GETCLIENTAREAANIMATION); always on elsewhere."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        value = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except (AttributeError, OSError):
        pass
    return True


class _Clock(QObject):
    """The shared frame timer; it stops when no spring is moving."""

    def __init__(self):
        super().__init__()
        self.springs: set[Spring] = set()
        self.timer = QTimer(self)
        self.timer.setInterval(_FRAME_MS)
        self.timer.timeout.connect(self._tick)
        self.last = 0.0

    def add(self, spring: Spring) -> None:
        if not self.springs:
            self.last = time.perf_counter()
            self.timer.start()
        self.springs.add(spring)

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.05, max(0.001, now - self.last))  # a stalled frame must not make springs jump
        self.last = now
        for spring in list(self.springs):
            try:
                moving = shiboken6.isValid(spring) and spring._step(dt)
            except RuntimeError:  # its widget was deleted during the frame
                moving = False
            if not moving:
                self.springs.discard(spring)
        if not self.springs:
            self.timer.stop()


_clock: _Clock | None = None


def _shared_clock() -> _Clock:
    global _clock
    if _clock is None:
        _clock = _Clock()
    return _clock


class Spring(QObject):
    """A value that follows its target like a spring; `changed` is emitted on every frame."""

    changed = Signal(float)

    def __init__(self, value: float = 0.0, response: float = 0.32, damping: float = 1.0,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.value = float(value)
        self.target = float(value)
        self.velocity = 0.0
        self.response = response
        self.damping = damping

    def set_target(self, target: float, *, immediate: bool = False) -> None:
        target = float(target)
        if immediate or not animations_enabled():
            self.jump(target)
            return
        if target == self.target and not self.moving():
            return
        self.target = target
        _shared_clock().add(self)

    def jump(self, value: float) -> None:
        """Goes to `value` at once (no motion)."""
        self.value = self.target = float(value)
        self.velocity = 0.0
        if _clock is not None:
            _clock.springs.discard(self)
        self.changed.emit(self.value)

    def moving(self) -> bool:
        return _clock is not None and self in _clock.springs

    def _step(self, dt: float) -> bool:
        """Advances by `dt` seconds; False once settled. Stiffness and damping follow Apple's
        parameterization: k = (2π / response)², c = 4π · damping / response (unit mass)."""
        k = (2 * math.pi / self.response) ** 2
        c = 4 * math.pi * self.damping / self.response
        steps = max(1, int(math.ceil(dt / 0.004)))  # small substeps keep stiff springs stable
        h = dt / steps
        for _ in range(steps):
            accel = -k * (self.value - self.target) - c * self.velocity
            self.velocity += accel * h
            self.value += self.velocity * h
        span = max(1.0, abs(self.target))
        if abs(self.value - self.target) < _EPS * span and abs(self.velocity) < _EPS * span * 10:
            self.value = self.target
            self.velocity = 0.0
            self.changed.emit(self.value)
            return False
        self.changed.emit(self.value)
        return True
