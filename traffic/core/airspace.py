import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

import numpy as np
import pyproj
from shapely.geometry import Polygon, base, mapping, polygon, shape
from shapely.ops import orient, transform, unary_union

from . import Flight, Traffic
from .lazy import lazy_evaluation
from .mixins import GeographyMixin, PointMixin, ShapelyMixin  # noqa: F401

if TYPE_CHECKING:
    from cartopy.mpl.geoaxes import GeoAxesSubplot
    from matplotlib.patches import Polygon as MplPolygon

    from shapely.geometry.base import BaseGeometry


class ExtrudedPolygon(NamedTuple):
    polygon: Polygon
    lower: Optional[float]
    upper: Optional[float]


class AirspaceInfo(NamedTuple):
    name: str
    type: Optional[str]


AirspaceList = List[ExtrudedPolygon]
components: Dict[str, Set[AirspaceInfo]] = defaultdict(set)


class Airspace(ShapelyMixin):
    def __init__(
        self,
        name: str,
        elements: List[ExtrudedPolygon],
        type_: Optional[str] = None,
        designator: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:

        self.elements: List[ExtrudedPolygon] = elements
        self.designator: Optional[str] = designator
        self.name: str = name
        self.type: Optional[str] = type_
        self.properties: Dict[str, Any] = (
            properties if properties is not None else {}
        )

    def flatten(self) -> Polygon:
        """Returns the 2D footprint of the airspace."""
        return orient(unary_union([p.polygon for p in self]), -1)

    @property
    def shape(self) -> "BaseGeometry":
        return self.flatten()

    def __getitem__(self, *args: int) -> ExtrudedPolygon:
        return self.elements.__getitem__(*args)

    def __add__(self, other: Union[Literal[0], "Airspace"]) -> "Airspace":
        if other == 0:
            # useful for compatibility with sum() function
            return self

        union = unary_union_with_alt(list(self) + list(other))
        new_name = (
            self.name
            if self.name == other.name
            else f"{self.name}, {other.name}"
        )
        new_designator = (
            self.designator if self.designator is not None else other.designator
        )
        new_type = self.type if self.type is not None else other.type

        return Airspace(
            name=new_name,
            elements=union,
            type_=new_type,
            designator=new_designator,
            properties={**self.properties, **other.properties},
        )

    def __radd__(self, other: Union[Literal[0], "Airspace"]) -> "Airspace":
        return self + other

    def __iter__(self) -> Iterator[ExtrudedPolygon]:
        return self.elements.__iter__()

    def _repr_html_(self) -> str:
        title = f"<b>{self.name} [{self.designator}] ({self.type})</b>"
        shapes = ""
        title += "<ul>"

        bounds = self.bounds
        projection = pyproj.Proj(
            proj="aea",  # equivalent projection
            lat_1=bounds[1],
            lat_2=bounds[3],
            lat_0=(bounds[1] + bounds[3]) / 2,
            lon_0=(bounds[0] + bounds[2]) / 2,
        )

        for polygon_ in self:
            transformer = pyproj.Transformer.from_proj(
                pyproj.Proj("epsg:4326"), projection, always_xy=True
            )
            projected_shape = transform(transformer.transform, polygon_.polygon)
            title += f"<li>{polygon_.lower}, {polygon_.upper}</li>"
            shapes += projected_shape.simplify(1e3)._repr_svg_()
        title += "</ul>"
        no_wrap_div = '<div style="white-space: nowrap; width: 12%">{}</div>'
        return title + no_wrap_div.format(shapes)

    def __repr__(self) -> str:
        return f"Airspace {self.name} [{self.designator}] ({self.type})"

    def __str__(self) -> str:
        return f"""Airspace {self.name} with {len(self.elements)} parts"""

    def annotate(
        self, ax: "GeoAxesSubplot", **kwargs: Any
    ) -> None:  # coverage: ignore
        from cartopy.crs import PlateCarree

        if "projection" in ax.__dict__:
            kwargs["transform"] = PlateCarree()
        if "s" not in kwargs:
            kwargs["s"] = self.name
        ax.text(*np.array(self.centroid), **kwargs)

    def plot(
        self, ax: "GeoAxesSubplot", **kwargs: Any
    ) -> None:  # coverage: ignore
        flat = self.flatten()
        if isinstance(flat, base.BaseMultipartGeometry):
            for poly in flat:
                # quick and dirty
                sub = Airspace("", [ExtrudedPolygon(poly, 0, 0)])
                sub.plot(ax, **kwargs)
            return

        if "facecolor" not in kwargs:
            kwargs["facecolor"] = "None"
        if "edgecolor" not in kwargs:
            kwargs["edgecolor"] = ax._get_lines.get_next_color()

        if "projection" in ax.__dict__:
            from cartopy.crs import PlateCarree

            ax.add_geometries([flat], crs=PlateCarree(), **kwargs)
        else:
            ax.add_patch(MplPolygon(list(flat.exterior.coords), **kwargs))

    @property
    def point(self) -> PointMixin:
        p = PointMixin()
        p.longitude, p.latitude = list(self.centroid.coords)[0]
        return p

    @property
    def components(self) -> Set[AirspaceInfo]:
        return components[self.name]

    def decompose(self, extr_p: ExtrudedPolygon) -> Iterator[Polygon]:
        c = np.stack(extr_p.polygon.exterior.coords)
        alt = np.zeros(c.shape[0], dtype=float)
        assert extr_p.upper is not None
        assert extr_p.lower is not None

        alt[:] = min(extr_p.upper, 400) * 30.48
        upper_layer = np.c_[c, alt]
        yield Polygon(upper_layer)
        alt[:] = max(0, extr_p.lower) * 30.48
        lower_layer = np.c_[c, alt][::-1, :]
        yield Polygon(lower_layer)

        for i, j in zip(range(c.shape[0] - 1), range(c.shape[0], 1, -1)):
            yield Polygon(
                np.r_[
                    lower_layer[i : i + 2, :],  # noqa: E203
                    upper_layer[j - 2 : j, :],  # noqa: E203
                ]
            )

    def above(self, level: int) -> "Airspace":
        return Airspace(
            self.name,
            list(
                c
                for c in self.elements
                if c.upper is not None and c.upper >= level
            ),
            type_=self.type,
        )

    def below(self, level: int) -> "Airspace":
        return Airspace(
            self.name,
            list(
                c
                for c in self.elements
                if c.lower is not None and c.lower <= level
            ),
            type_=self.type,
        )

    def export_json(self) -> Dict[str, Any]:
        export: Dict[str, Any] = {"name": self.name, "type": self.type}
        shapes = []
        for p in self:
            shapes.append(
                {
                    "upper": p.upper,
                    "lower": p.lower,
                    "polygon": mapping(p.polygon),
                }
            )
        export["shapes"] = shapes
        return export

    @classmethod
    def from_json(cls, json: Dict[str, Any]) -> "Airspace":
        return cls(
            name=json["name"],
            type_=json["type"],
            elements=[
                ExtrudedPolygon(
                    polygon=polygon.orient(shape(layer["polygon"]), -1),
                    upper=layer["upper"],
                    lower=layer["lower"],
                )
                for layer in json["shapes"]
            ],
        )

    @classmethod
    def from_file(cls, filename: Union[Path, str]) -> "Airspace":
        path = Path(filename)
        with path.open("r") as fh:
            return cls.from_json(json.load(fh))


def unary_union_with_alt(polyalt: AirspaceList) -> AirspaceList:
    altitudes = set(
        alt for _, *low_up in polyalt for alt in low_up if alt is not None
    )
    slices = sorted(altitudes)
    if len(slices) == 1 and slices[0] is None:
        simple_union = unary_union([p for p, *_ in polyalt])
        return [ExtrudedPolygon(simple_union, float("-inf"), float("inf"))]
    results: List[ExtrudedPolygon] = []
    for low, up in zip(slices, slices[1:]):
        matched_poly = [
            p
            for (p, low_, up_) in polyalt
            if low_ is not None
            and up_ is not None
            and low_ <= low <= up_
            and low_ <= up <= up_
        ]
        new_poly = ExtrudedPolygon(unary_union(matched_poly), low, up)
        if len(results) > 0 and new_poly.polygon.equals(results[-1].polygon):
            merged = ExtrudedPolygon(new_poly.polygon, results[-1].lower, up)
            results[-1] = merged
        else:
            results.append(new_poly)
    return results


# -- Methods below are placed here because of possible circular imports --


T = TypeVar("T", bound="GeographyMixin")


def inside_bbox(
    geography: T,
    bounds: Union[
        ShapelyMixin, base.BaseGeometry, Tuple[float, float, float, float]
    ],
) -> Optional[T]:
    """Returns the part of the DataFrame with coordinates located within the
    bounding box of the shape passed in parameter.

        The bounds parameter can be:

        - an Airspace,
        - a shapely Geometry,
        - a tuple of floats (west, south, east, north)

    """

    if isinstance(bounds, Airspace):
        bounds = bounds.flatten().bounds

    elif isinstance(bounds, base.BaseGeometry):
        bounds = bounds.bounds

    elif hasattr(bounds, "shape"):
        bounds = bounds.shape.bounds  # type: ignore

    west, south, east, north = bounds

    query = "{0} <= longitude <= {2} and {1} <= latitude <= {3}"
    query = query.format(*bounds)

    return geography.query(query)


def _flight_intersects(
    flight: Flight, shape: Union[ShapelyMixin, base.BaseGeometry]
) -> bool:
    """Returns True if the trajectory is inside the given shape.

    - If an Airspace is passed, the 3D trajectory is compared to each layers
      constituting the airspace, with corresponding altitude limits.
    - If a shapely Geometry is passed, the 2D trajectory alone is
    considered.
    """
    linestring = flight.linestring
    if linestring is None:
        return False
    if isinstance(shape, base.BaseGeometry):
        return not linestring.intersection(shape).is_empty
    if not isinstance(shape, Airspace):  # i.e. ShapelyMixin
        return not linestring.intersection(shape.shape).is_empty
    for layer in shape:
        ix = linestring.intersection(layer.polygon)
        if not ix.is_empty:
            assert layer.lower is not None
            assert layer.upper is not None
            if isinstance(ix, base.BaseMultipartGeometry):
                for part in ix:
                    if any(
                        100 * layer.lower < x[2] < 100 * layer.upper
                        for x in part.coords
                    ):
                        return True
            else:
                if any(
                    100 * layer.lower < x[2] < 100 * layer.upper
                    for x in ix.coords
                ):
                    return True
    return False


# -- The ugly monkey-patching --

setattr(Flight, "inside_bbox", inside_bbox)
setattr(Traffic, "inside_bbox", lazy_evaluation(default=True)(inside_bbox))

setattr(Flight, "intersects", _flight_intersects)
