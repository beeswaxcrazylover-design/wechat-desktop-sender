"""Pure placement logic. All rectangles are fresh physical screen coordinates."""
from dataclasses import dataclass


class PlacementError(RuntimeError):
    pass


def rect(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(type(n) is not int for n in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise PlacementError('NOTICE_INVALID_RECTANGLE')
    return tuple(value)


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


@dataclass(frozen=True)
class PlacementGeometry:
    work_areas: tuple
    protected: tuple
    coordinate_space: str = 'physical_screen_pixels'

    def __post_init__(self):
        if self.coordinate_space != 'physical_screen_pixels':
            raise PlacementError('NOTICE_PHYSICAL_COORDINATES_REQUIRED')
        if not self.work_areas or not self.protected:
            raise PlacementError('NOTICE_FRESH_TARGET_GEOMETRY_REQUIRED')
        object.__setattr__(self, 'work_areas', tuple(rect(r) for r in self.work_areas))
        object.__setattr__(self, 'protected', tuple(rect(r) for r in self.protected))


def choose_rect(size, geometry, *, previous=None, gap=8):
    """Find an actual free rectangle, preserving the current position when safe.

    Work areas exclude taskbars. Protected rectangles must also include the
    complete target window (the existing sender checks whole-window occlusion),
    any related popups, and any additional capture/input regions. No whitelist.
    """
    if not isinstance(geometry, PlacementGeometry):
        raise PlacementError('NOTICE_FRESH_TARGET_GEOMETRY_REQUIRED')
    width, height = size
    if any(type(n) is not int or n <= 0 for n in size) or type(gap) is not int or gap < 0:
        raise PlacementError('NOTICE_INVALID_SIZE')
    obstacles = [(a-gap, b-gap, c+gap, d+gap) for a, b, c, d in geometry.protected]
    def safe(candidate):
        return (any(contains(area, candidate) for area in geometry.work_areas)
                and not any(intersects(candidate, obstacle) for obstacle in obstacles))
    if previous is not None:
        previous = rect(previous)
        if (previous[2]-previous[0], previous[3]-previous[1]) == (width, height) and safe(previous):
            return previous
    for left, top, right, bottom in geometry.work_areas:
        xs = {left+gap, right-width-gap}
        ys = {top+gap, bottom-height-gap}
        for a, b, c, d in obstacles:
            xs.update((a-width, c))
            ys.update((b-height, d))
        # Prefer a top-right edge slot; no cursor movement or guessed OCR holes.
        for y in sorted(ys):
            for x in sorted(xs, reverse=True):
                candidate = (x, y, x+width, y+height)
                if safe(candidate):
                    return candidate
    raise PlacementError('NOTICE_NO_SAFE_VISIBLE_SPACE')
