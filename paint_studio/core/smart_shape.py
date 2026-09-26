from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Protocol, Sequence


class StrokePoint(Protocol):
    x: float
    y: float
    pressure: float
    timestamp_ms: float


@dataclass(frozen=True, slots=True)
class CleanPoint:
    x: float
    y: float
    pressure: float = 1.0
    timestamp_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class CleanShape:
    points: tuple[CleanPoint, ...]
    closed: bool
    center_x: float
    center_y: float
    anchor_x: float
    anchor_y: float
    kind: str
    corner_count: int
    straight_segment_count: int
    source_point_count: int
    elapsed_ms: float

    def adjusted(self, x: float, y: float) -> tuple[CleanPoint, ...]:
        """Scale and rotate the clean path from its held endpoint."""
        pivot_x = self.center_x if self.closed else self.points[0].x
        pivot_y = self.center_y if self.closed else self.points[0].y
        base_x = self.anchor_x - pivot_x
        base_y = self.anchor_y - pivot_y
        next_x = float(x) - pivot_x
        next_y = float(y) - pivot_y
        base_length = math.hypot(base_x, base_y)
        next_length = math.hypot(next_x, next_y)
        if base_length <= 1e-6 or next_length <= 1e-6:
            return self.points
        scale = max(0.04, min(25.0, next_length / base_length))
        angle = math.atan2(next_y, next_x) - math.atan2(base_y, base_x)
        cosine = math.cos(angle) * scale
        sine = math.sin(angle) * scale
        return tuple(
            CleanPoint(
                pivot_x + (point.x - pivot_x) * cosine - (point.y - pivot_y) * sine,
                pivot_y + (point.x - pivot_x) * sine + (point.y - pivot_y) * cosine,
                point.pressure,
                point.timestamp_ms,
            )
            for point in self.points
        )


def clean_stroke(samples: Sequence[StrokePoint]) -> CleanShape | None:
    """Clean path for a held stroke, keeps corners, straightens flat runs and smooths curves."""
    started = time.perf_counter()
    source = _deduplicate(samples)
    if len(source) < 4:
        return None
    source_length = _path_length(source)
    bounds = _bounds(source)
    diagonal = math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1])
    if source_length < 12.0 or diagonal < 4.0:
        return None

    end_gap = _distance(source[0], source[-1])
    close_distance = max(9.0, min(diagonal * 0.35, source_length * 0.10))
    closed = end_gap <= close_distance and source_length >= diagonal * 1.55
    dense = _resample(source, closed=closed)
    if len(dense) < 5:
        return None

    corners = _corner_indices(dense, closed=closed)
    if closed and end_gap > max(4.0, diagonal * 0.06):
        corners = _merge_closure_artifacts(dense, corners)
    general = _smooth_preserving_corners(dense, corners, closed=closed)
    general, straight_count, straight_coverage = _straighten_runs(general, corners, closed=closed)
    required_straight = 3 if len(corners) <= 4 else max(3, math.ceil(len(corners) * 0.5))
    polygon_evidence = straight_count >= required_straight and straight_coverage >= 0.42
    ellipse = _fit_ellipse(dense) if closed and not polygon_evidence else None
    if ellipse is not None:
        cleaned = _ellipse_points(dense, ellipse)
        kind = "ellipse"
        straight_count = 0
        corners = []
    else:
        cleaned = general
        if closed and corners and straight_count == len(corners):
            kind = "polygon"
        elif corners and straight_count:
            kind = "mixed"
        elif corners:
            kind = "cornered"
        else:
            kind = "curve"

    if closed:
        first = cleaned[0]
        last_source = source[-1]
        cleaned = [
            *cleaned,
            CleanPoint(first.x, first.y, last_source.pressure, last_source.timestamp_ms),
        ]
    elif _is_straight_enough(cleaned):
        cleaned = _straight_line(cleaned)
        kind = "line"
        straight_count = 1

    clean_bounds = _bounds(cleaned)
    center_x = (clean_bounds[0] + clean_bounds[2]) * 0.5
    center_y = (clean_bounds[1] + clean_bounds[3]) * 0.5
    anchor = source[-1]
    return CleanShape(
        tuple(cleaned),
        closed,
        center_x,
        center_y,
        anchor.x,
        anchor.y,
        kind,
        len(corners),
        straight_count,
        len(source),
        (time.perf_counter() - started) * 1000.0,
    )


def _deduplicate(samples: Sequence[StrokePoint]) -> list[CleanPoint]:
    result: list[CleanPoint] = []
    for sample in samples:
        point = CleanPoint(
            float(sample.x),
            float(sample.y),
            max(0.01, min(1.0, float(sample.pressure))),
            float(sample.timestamp_ms),
        )
        if result and _distance(result[-1], point) < 0.18:
            result[-1] = point
        else:
            result.append(point)
    return result


def _path_length(points: Sequence[CleanPoint], *, closed: bool = False) -> float:
    length = sum(_distance(first, second) for first, second in zip(points, points[1:]))
    if closed and len(points) > 2:
        length += _distance(points[-1], points[0])
    return length


def _bounds(points: Sequence[CleanPoint]) -> tuple[float, float, float, float]:
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


def _distance(first: CleanPoint, second: CleanPoint) -> float:
    return math.hypot(second.x - first.x, second.y - first.y)


def _lerp(first: CleanPoint, second: CleanPoint, amount: float) -> CleanPoint:
    return CleanPoint(
        first.x + (second.x - first.x) * amount,
        first.y + (second.y - first.y) * amount,
        first.pressure + (second.pressure - first.pressure) * amount,
        first.timestamp_ms + (second.timestamp_ms - first.timestamp_ms) * amount,
    )


def _resample(points: Sequence[CleanPoint], *, closed: bool) -> list[CleanPoint]:
    source = list(points)
    if closed and _distance(source[-1], source[0]) > 1e-6:
        source.append(
            CleanPoint(source[0].x, source[0].y, source[-1].pressure, source[-1].timestamp_ms)
        )
    total = _path_length(source)
    target_count = max(24 if closed else 12, min(192, round(total / 2.25)))
    spacing = total / target_count
    result = [source[0]]
    traveled = 0.0
    target = spacing
    for first, second in zip(source, source[1:]):
        segment = _distance(first, second)
        if segment <= 1e-9:
            continue
        while traveled + segment >= target and len(result) < target_count:
            result.append(_lerp(first, second, (target - traveled) / segment))
            target += spacing
        traveled += segment
    if not closed and _distance(result[-1], source[-1]) > 1e-6:
        result.append(source[-1])
    return result


def _corner_indices(points: Sequence[CleanPoint], *, closed: bool) -> list[int]:
    count = len(points)
    if count < 7:
        return []
    probe = _corner_probe(points, closed=closed)
    small_span = max(2, min(4, count // 36))
    large_span = max(small_span + 2, min(9, count // 18))
    first = 0 if closed else large_span
    last = count if closed else count - large_span
    candidates: list[tuple[float, int]] = []
    for index in range(first, last):
        small = _turn(probe, index, small_span, closed)
        large = _turn(probe, index, large_span, closed)
        concentration = small / max(large, 1e-6)
        if small >= math.radians(18.0) and large >= math.radians(27.0) and concentration >= 0.42:
            score = small * 0.72 + large * 0.28
            candidates.append((score, index))

    bounds = _bounds(probe)
    diagonal = math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1])
    macro_span = max(large_span, min(18, count // 11))
    for index in _rdp_indices(probe, max(1.25, diagonal * 0.012), closed=closed):
        if not closed and index in (0, count - 1):
            continue
        small = _turn(probe, index, small_span, closed)
        macro = _turn(probe, index, macro_span, closed)
        if small >= math.radians(9.0) and macro >= math.radians(24.0):
            candidates.append((math.pi + small * 0.4 + macro * 0.6, index))

    separation = max(5, count // 24)
    selected: list[int] = []
    for _score, index in sorted(candidates, reverse=True):
        if all(_cyclic_index_distance(index, other, count, closed) > separation for other in selected):
            selected.append(index)
    if closed:
        selected = _recover_skipped_corners(selected, candidates, count, separation)
    selected.sort()
    return _prune_corner_chain(probe, selected, closed=closed)


def _merge_closure_artifacts(points: Sequence[CleanPoint], corners: Sequence[int]) -> list[int]:
    result = sorted(corners)
    if len(result) < 4:
        return result
    count = len(points)
    gaps = [
        (result[(index + 1) % len(result)] - result[index]) % count
        for index in range(len(result))
    ]
    median_gap = sorted(gaps)[len(gaps) // 2]
    seam_limit = count * 0.18
    choices: list[tuple[int, int, int]] = []
    for index, gap in enumerate(gaps):
        first = result[index]
        second = result[(index + 1) % len(result)]
        near_seam = (
            first >= count - seam_limit
            or first <= seam_limit
            or second >= count - seam_limit
            or second <= seam_limit
        )
        if near_seam and gap < median_gap * 0.58:
            choices.append((gap, first, second))
    if not choices:
        return result
    _gap, first, second = min(choices)
    span = max(6, min(18, count // 12))
    first_turn = _turn(points, first, span, True)
    second_turn = _turn(points, second, span, True)
    result.remove(first if first_turn < second_turn else second)
    return result


def _recover_skipped_corners(
    selected: Sequence[int],
    candidates: Sequence[tuple[float, int]],
    count: int,
    separation: int,
) -> list[int]:
    result = sorted(set(selected))
    while len(result) >= 3:
        gaps = [
            (result[(index + 1) % len(result)] - result[index]) % count
            for index in range(len(result))
        ]
        median_gap = sorted(gaps)[len(gaps) // 2]
        widest_index = max(range(len(gaps)), key=gaps.__getitem__)
        if gaps[widest_index] <= median_gap * 1.6:
            break
        start = result[widest_index]
        end = result[(widest_index + 1) % len(result)]
        choices = [
            (score, index)
            for score, index in candidates
            if index not in result
            and max(2, separation // 2) < (index - start) % count < (end - start) % count
            and (end - index) % count > max(2, separation // 2)
        ]
        if not choices:
            break
        result.append(max(choices)[1])
        result.sort()
    return result


def _rdp_indices(points: Sequence[CleanPoint], epsilon: float, *, closed: bool) -> list[int]:
    count = len(points)
    if count < 3:
        return list(range(count))
    if not closed:
        return _rdp_open_indices(points, list(range(count)), epsilon)
    span = max(5, min(24, count // 9))
    seed = max(range(count), key=lambda index: _turn(points, index, span, True))
    opposite = max(range(count), key=lambda index: _distance(points[seed], points[index]))
    first_arc = _cyclic_indices(seed, opposite, count)
    second_arc = _cyclic_indices(opposite, seed, count)
    return sorted(
        set(
            _rdp_open_indices(points, first_arc, epsilon)
            + _rdp_open_indices(points, second_arc, epsilon)
        )
    )


def _cyclic_indices(start: int, end: int, count: int) -> list[int]:
    result = [start]
    while result[-1] != end:
        result.append((result[-1] + 1) % count)
    return result


def _rdp_open_indices(points: Sequence[CleanPoint], indices: list[int], epsilon: float) -> list[int]:
    if len(indices) <= 2:
        return indices
    start, end = points[indices[0]], points[indices[-1]]
    distance, split = max(
        ((_line_distance(points[index], start, end), offset) for offset, index in enumerate(indices[1:-1], 1)),
        default=(0.0, 0),
    )
    if distance <= epsilon:
        return [indices[0], indices[-1]]
    left = _rdp_open_indices(points, indices[: split + 1], epsilon)
    right = _rdp_open_indices(points, indices[split:], epsilon)
    return [*left[:-1], *right]


def _prune_corner_chain(
    points: Sequence[CleanPoint],
    corners: Sequence[int],
    *,
    closed: bool,
) -> list[int]:
    selected = list(corners)
    minimum = 3 if closed else 1
    changed = True
    while changed and len(selected) > minimum:
        changed = False
        anchors = selected if closed else [0, *selected, len(points) - 1]
        for corner in tuple(selected):
            anchor_index = anchors.index(corner)
            if not closed and anchor_index in (0, len(anchors) - 1):
                continue
            before = anchors[(anchor_index - 1) % len(anchors)]
            after = anchors[(anchor_index + 1) % len(anchors)]
            incoming_x = points[corner].x - points[before].x
            incoming_y = points[corner].y - points[before].y
            outgoing_x = points[after].x - points[corner].x
            outgoing_y = points[after].y - points[corner].y
            incoming_length = math.hypot(incoming_x, incoming_y)
            outgoing_length = math.hypot(outgoing_x, outgoing_y)
            if incoming_length <= 1e-6 or outgoing_length <= 1e-6:
                selected.remove(corner)
                changed = True
                break
            cosine = (incoming_x * outgoing_x + incoming_y * outgoing_y) / (incoming_length * outgoing_length)
            turn = math.acos(max(-1.0, min(1.0, cosine)))
            offset_distance = _line_distance(points[corner], points[before], points[after])
            if turn < math.radians(12.0) and offset_distance < max(
                1.25,
                min(incoming_length, outgoing_length) * 0.075,
            ):
                selected.remove(corner)
                changed = True
                break
    return sorted(selected)


def _corner_probe(points: Sequence[CleanPoint], *, closed: bool) -> list[CleanPoint]:
    result = list(points)
    count = len(result)
    for _pass in range(2):
        previous = result
        result = []
        for index, point in enumerate(previous):
            if not closed and index in (0, count - 1):
                result.append(point)
                continue
            before = previous[(index - 1) % count]
            after = previous[(index + 1) % count]
            result.append(
                CleanPoint(
                    point.x * 0.6 + (before.x + after.x) * 0.2,
                    point.y * 0.6 + (before.y + after.y) * 0.2,
                    point.pressure,
                    point.timestamp_ms,
                )
            )
    return result


def _turn(points: Sequence[CleanPoint], index: int, span: int, closed: bool) -> float:
    count = len(points)
    before_index = index - span
    after_index = index + span
    if closed:
        before_index %= count
        after_index %= count
    elif before_index < 0 or after_index >= count:
        return 0.0
    before = points[before_index]
    current = points[index]
    after = points[after_index]
    first_x, first_y = current.x - before.x, current.y - before.y
    second_x, second_y = after.x - current.x, after.y - current.y
    first_length = math.hypot(first_x, first_y)
    second_length = math.hypot(second_x, second_y)
    if first_length <= 1e-6 or second_length <= 1e-6:
        return 0.0
    cosine = (first_x * second_x + first_y * second_y) / (first_length * second_length)
    return math.acos(max(-1.0, min(1.0, cosine)))


def _cyclic_index_distance(first: int, second: int, count: int, closed: bool) -> int:
    direct = abs(first - second)
    return min(direct, count - direct) if closed else direct


def _smooth_preserving_corners(
    points: Sequence[CleanPoint],
    corners: Sequence[int],
    *,
    closed: bool,
) -> list[CleanPoint]:
    result = list(points)
    count = len(result)
    protected: dict[int, float] = {}
    for corner in corners:
        protected[corner] = 0.0
        protected[(corner - 1) % count] = min(protected.get((corner - 1) % count, 1.0), 0.22)
        protected[(corner + 1) % count] = min(protected.get((corner + 1) % count, 1.0), 0.22)
    if not closed:
        protected[0] = 0.0
        protected[count - 1] = 0.0
    for _pass in range(4):
        previous = result
        next_points: list[CleanPoint] = []
        for index, point in enumerate(previous):
            if (not closed and index in (0, count - 1)) or protected.get(index) == 0.0:
                next_points.append(point)
                continue
            before = previous[(index - 1) % count]
            after = previous[(index + 1) % count]
            strength = 0.5 * protected.get(index, 1.0)
            average_x = (before.x + after.x) * 0.5
            average_y = (before.y + after.y) * 0.5
            next_points.append(
                CleanPoint(
                    point.x + (average_x - point.x) * strength,
                    point.y + (average_y - point.y) * strength,
                    point.pressure,
                    point.timestamp_ms,
                )
            )
        result = next_points
    return result


def _straighten_runs(
    points: Sequence[CleanPoint],
    corners: Sequence[int],
    *,
    closed: bool,
) -> tuple[list[CleanPoint], int, float]:
    result = list(points)
    count = len(result)
    anchors = list(corners)
    if not closed:
        anchors = sorted({0, *anchors, count - 1})
    if len(anchors) < 2:
        return result, 0, 0.0
    runs = list(zip(anchors, anchors[1:]))
    if closed:
        runs.append((anchors[-1], anchors[0] + count))
    straight_count = 0
    straight_length = 0.0
    for start, end in runs:
        indices = [index % count for index in range(start, end + 1)]
        if len(indices) < 3:
            continue
        first = result[indices[0]]
        last = result[indices[-1]]
        line_length = _distance(first, last)
        if line_length < 5.0:
            continue
        distances = [_line_distance(result[index], first, last) for index in indices[1:-1]]
        if not distances:
            continue
        rms = math.sqrt(sum(value * value for value in distances) / len(distances))
        if rms > max(1.8, line_length * 0.03) or max(distances) > max(4.0, line_length * 0.075):
            continue
        total_steps = len(indices) - 1
        for step, index in enumerate(indices[1:-1], 1):
            original = result[index]
            amount = step / total_steps
            result[index] = CleanPoint(
                first.x + (last.x - first.x) * amount,
                first.y + (last.y - first.y) * amount,
                original.pressure,
                original.timestamp_ms,
            )
        straight_count += 1
        straight_length += sum(_distance(result[first_index], result[second_index]) for first_index, second_index in zip(indices, indices[1:]))
    coverage = straight_length / max(_path_length(points, closed=closed), 1e-6)
    return result, straight_count, coverage


def _line_distance(point: CleanPoint, start: CleanPoint, end: CleanPoint) -> float:
    dx, dy = end.x - start.x, end.y - start.y
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return _distance(point, start)
    return abs(dy * point.x - dx * point.y + end.x * start.y - end.y * start.x) / length


@dataclass(frozen=True, slots=True)
class _Ellipse:
    center_x: float
    center_y: float
    radius_x: float
    radius_y: float
    angle: float
    start_angle: float
    direction: float


def _fit_ellipse(points: Sequence[CleanPoint]) -> _Ellipse | None:
    mean_x = sum(point.x for point in points) / len(points)
    mean_y = sum(point.y for point in points) / len(points)
    xx = sum((point.x - mean_x) ** 2 for point in points) / len(points)
    yy = sum((point.y - mean_y) ** 2 for point in points) / len(points)
    xy = sum((point.x - mean_x) * (point.y - mean_y) for point in points) / len(points)
    angle = 0.5 * math.atan2(2.0 * xy, xx - yy)
    cosine, sine = math.cos(angle), math.sin(angle)
    local = [
        (
            (point.x - mean_x) * cosine + (point.y - mean_y) * sine,
            -(point.x - mean_x) * sine + (point.y - mean_y) * cosine,
        )
        for point in points
    ]
    minimum_x = min(point[0] for point in local)
    maximum_x = max(point[0] for point in local)
    minimum_y = min(point[1] for point in local)
    maximum_y = max(point[1] for point in local)
    radius_x = (maximum_x - minimum_x) * 0.5
    radius_y = (maximum_y - minimum_y) * 0.5
    if radius_x < 3.0 or radius_y < 3.0:
        return None
    local_center_x = (minimum_x + maximum_x) * 0.5
    local_center_y = (minimum_y + maximum_y) * 0.5
    center_x = mean_x + local_center_x * cosine - local_center_y * sine
    center_y = mean_y + local_center_x * sine + local_center_y * cosine
    radial_errors: list[float] = []
    for x, y in local:
        normalized = math.hypot((x - local_center_x) / radius_x, (y - local_center_y) / radius_y)
        radial_errors.append(abs(normalized - 1.0))
    rms = math.sqrt(sum(value * value for value in radial_errors) / len(radial_errors))
    if rms > 0.115 or max(radial_errors) > 0.32:
        return None
    first_x = (points[0].x - center_x) * cosine + (points[0].y - center_y) * sine
    first_y = -(points[0].x - center_x) * sine + (points[0].y - center_y) * cosine
    start_angle = math.atan2(first_y / radius_y, first_x / radius_x)
    signed_area = sum(
        first.x * second.y - second.x * first.y
        for first, second in zip(points, (*points[1:], points[0]))
    )
    return _Ellipse(center_x, center_y, radius_x, radius_y, angle, start_angle, 1.0 if signed_area >= 0 else -1.0)


def _ellipse_points(points: Sequence[CleanPoint], ellipse: _Ellipse) -> list[CleanPoint]:
    count = max(48, min(192, len(points)))
    cosine, sine = math.cos(ellipse.angle), math.sin(ellipse.angle)
    result: list[CleanPoint] = []
    for index in range(count):
        angle = ellipse.start_angle + ellipse.direction * math.tau * index / count
        local_x = math.cos(angle) * ellipse.radius_x
        local_y = math.sin(angle) * ellipse.radius_y
        source = points[min(len(points) - 1, round(index * (len(points) - 1) / max(1, count - 1)))]
        result.append(
            CleanPoint(
                ellipse.center_x + local_x * cosine - local_y * sine,
                ellipse.center_y + local_x * sine + local_y * cosine,
                source.pressure,
                source.timestamp_ms,
            )
        )
    return result


def _is_straight_enough(points: Sequence[CleanPoint]) -> bool:
    if len(points) < 3:
        return True
    start, end = points[0], points[-1]
    direct = _distance(start, end)
    if direct < 6.0:
        return False
    length = _path_length(points)
    distances = [_line_distance(point, start, end) for point in points[1:-1]]
    rms = math.sqrt(sum(value * value for value in distances) / max(1, len(distances)))
    return direct / max(length, 1e-6) >= 0.88 and rms <= max(1.5, direct * 0.025)


def _straight_line(points: Sequence[CleanPoint]) -> list[CleanPoint]:
    start, end = points[0], points[-1]
    count = max(2, len(points))
    return [
        CleanPoint(
            start.x + (end.x - start.x) * index / (count - 1),
            start.y + (end.y - start.y) * index / (count - 1),
            points[min(len(points) - 1, index)].pressure,
            points[min(len(points) - 1, index)].timestamp_ms,
        )
        for index in range(count)
    ]
