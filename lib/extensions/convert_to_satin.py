import inkex
from shapely import geometry as shgeo
from itertools import chain
import numpy
from numpy import diff, sign, setdiff1d
from scipy.signal import argrelmin
import math

from .base import InkstitchExtension
from ..svg.tags import SVG_PATH_TAG
from ..svg import get_correction_transform, PIXELS_PER_MM
from ..elements import Stroke
from ..utils import Point


class ConvertToSatin(InkstitchExtension):
    """Convert a line to a satin column of the same width."""

    def effect(self):
        if not self.get_elements():
            return

        if not self.selected:
            inkex.errormsg(_("Please select at least one line to convert to a satin column."))
            return

        if not all(isinstance(item, Stroke) for item in self.elements):
            # L10N: Convert To Satin extension, user selected one or more objects that were not lines.
            inkex.errormsg(_("Only simple lines may be converted to satin columns."))
            return

        for element in self.elements:
            parent = element.node.getparent()
            index = parent.index(element.node)
            correction_transform = get_correction_transform(element.node)

            for path in element.paths:
                try:
                    rails, rungs = self.path_to_satin(path, element.stroke_width)
                except ValueError:
                    inkex.errormsg(_("Cannot convert %s to a satin column because it intersects itself.  Try breaking it up into multiple paths.") % element.node.get('id'))

                parent.insert(index, self.satin_to_svg_node(rails, rungs, correction_transform))

            parent.remove(element.node)

    def path_to_satin(self, path, stroke_width):
        path = shgeo.LineString(path)

        left_rail = path.parallel_offset(stroke_width / 2.0, 'left')
        right_rail = path.parallel_offset(stroke_width / 2.0, 'right')

        if not isinstance(left_rail, shgeo.LineString) or \
           not isinstance(right_rail, shgeo.LineString):
                # If the parallel offsets come out as anything but a LineString, that means the
                # path intersects itself, when taking its stroke width into consideration.  See
                # the last example for parallel_offset() in the Shapely documentation:
                #   https://shapely.readthedocs.io/en/latest/manual.html#object.parallel_offset
                raise ValueError()

        # for whatever reason, shapely returns a right-side offset's coordinates in reverse
        left_rail = list(left_rail.coords)
        right_rail = list(reversed(right_rail.coords))

        rungs = self.generate_rungs(path, stroke_width)

        return (left_rail, right_rail), rungs

    def get_scores(self, path):
        """Generate an array of "scores" of the sharpness of corners in a path

        A higher score means that there are sharper corners in that section of
        the path.  We'll divide the path into boxes, with the score in each
        box indicating the sharpness of corners at around that percentage of
        the way through the path.  For example, if scores[40] is 100 and
        scores[45] is 200, then the path has sharper corners at a spot 45%
        along its length than at a spot 40% along its length.
        """

        # need 101 boxes in order to encompass percentages from 0% to 100%
        scores = numpy.zeros(101, numpy.int32)
        path_length = path.length

        prev_point = None
        prev_direction = None
        length_so_far = 0
        for point in path.coords:
            point = Point(*point)

            if prev_point is None:
                prev_point = point
                continue

            direction = (point - prev_point).unit()

            if prev_direction is not None:
                # The dot product of two vectors is |v1| * |v2| * cos(angle).
                # These are unit vectors, so their magnitudes are 1.
                cos_angle_between = prev_direction * direction
                angle = abs(math.degrees(math.acos(cos_angle_between)))

                # Use the square of the angle, measured in degrees.
                #
                # Why the square?  This penalizes bigger angles more than
                # smaller ones.
                #
                # Why degrees?  This is kind of arbitrary but allows us to
                # use integer math effectively and avoid taking the square
                # of a fraction between 0 and 1.
                scores[int(round(length_so_far / path_length * 100.0))] += angle ** 2

            length_so_far += (point - prev_point).length()
            prev_direction = direction
            prev_point = point

        return scores

    def local_minima(self, array):
        # from: https://stackoverflow.com/a/9667121/4249120
        # This finds spots where the curvature (second derivative) is > 0.
        #
        # This method has the convenient benefit of choosing points around
        # 5% before and after a sharp corner such as in a square.
        return (diff(sign(diff(array))) > 0).nonzero()[0] + 1

    def generate_rungs(self, path, stroke_width):
        """Create rungs for a satin column.

        Where should we put the rungs along a path?  We want to ensure that the
        resulting satin matches the original path as closely as possible.  We
        want to avoid having a ton of rungs that will annoy the user.  We want
        to ensure that the rungs we choose actually intersect both rails.

        We'll place a few rungs perpendicular to the tangent of the path.
        Things get pretty tricky at sharp corners.  If we naively place a rung
        perpendicular to the path just on either side of a sharp corner, the
        rung may not intersect both paths:
                       |    |
        _______________|    |
                      ______|_
        ____________________|

        It'd be best to place rungs in the straight sections before and after
        the sharp corner and allow the satin column to bend the stitches around
        the corner automatically.

        How can we find those spots?

        The general algorithm below is:

          * assign a "score" to each section of the path based on how sharp its
            corners are (higher means a sharper corner)
          * pick spots with lower scores
        """

        scores = self.get_scores(path)

        # This is kind of like a 1-dimensional gaussian blur filter.  We want to
        # avoid the area near a sharp corner, so we spread out its effect for
        # 5 buckets in either direction.
        scores = numpy.convolve(scores, [1, 2, 4, 8, 16, 8, 4, 2, 1], mode='same')

        # Now we'll find the spots that aren't near corners, whose scores are
        # low -- the local minima.
        rung_locations = self.local_minima(scores)

        # Remove the start and end, because we can't stick a rung there.
        rung_locations = setdiff1d(rung_locations, [0, 100])

        if len(rung_locations) == 0:
            # Straight lines won't have local minima, so add a rung in the center.
            rung_locations = [50]

        rungs = []
        last_rung_center = None

        for location in rung_locations:
            # Convert percentage to a fraction so that we can use interpolate's
            # normalized parameter.
            location = location / 100.0

            rung_center = path.interpolate(location, normalized=True)
            rung_center = Point(rung_center.x, rung_center.y)

            # Avoid placing rungs too close together.  This somewhat
            # arbitrarily rejects the rung if there was one less than 2
            # millimeters before this one.
            if last_rung_center is not None and \
               (rung_center - last_rung_center).length() < 2 * PIXELS_PER_MM:
                    continue
            else:
                last_rung_center = rung_center

            # We need to know the tangent of the path's curve at this point.
            # Pick another point just after this one and subtract them to
            # approximate a tangent vector.
            tangent_end = path.interpolate(location + 0.001, normalized=True)
            tangent_end = Point(tangent_end.x, tangent_end.y)
            tangent = (tangent_end - rung_center).unit()

            # Rotate 90 degrees left to make a normal vector.
            normal = tangent.rotate_left()

            # Travel 75% of the stroke width left and right to make the rung's
            # endpoints.  This means the rung's length is 150% of the stroke
            # width.
            offset = normal * stroke_width * 0.75
            rung_start = rung_center + offset
            rung_end = rung_center - offset

            rungs.append((rung_start.as_tuple(), rung_end.as_tuple()))

        return rungs


    def satin_to_svg_node(self, rails, rungs, correction_transform):
        d = ""
        for path in chain(rails, rungs):
            d += "M"
            for x, y in path:
                d += "%s,%s " % (x, y)
            d += " "

        return inkex.etree.Element(SVG_PATH_TAG,
            {
                "id": self.uniqueId("path"),
                "style": "stroke:#000000;stroke-width:1px;fill:none",
                "transform": correction_transform,
                "d": d,
                "embroider_satin_column": "true",
            }
        )