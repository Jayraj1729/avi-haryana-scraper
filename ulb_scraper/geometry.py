'''Bounded, overlapping map coverage using the portal's WGS84 geometry.'''

# Standard Library Imports
import logging
import math

# Third Party Imports
from shapely import from_wkt, make_valid
from shapely.geometry import Point, box

from scraper import DEFAULTVIEWPORTLAT, DEFAULTVIEWPORTLON


def readBoundary(wkt):
    '''
    Read and validate a polygon or multipolygon, including interior holes.

    Inputs:    <wkt>  -  WGS84 boundary returned by the portal

    Output:    Shapely polygon geometry
    '''
    logging.debug('Reading colony boundary')
    geometry = from_wkt(wkt)
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if geometry.is_empty or geometry.geom_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Colony has no usable polygon boundary')
    west, south, east, north = geometry.bounds
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError('Boundary coordinates are not WGS84 longitude/latitude')
    return geometry


def seedBounds(geometry):
    '''
    Yield overlapping viewports intersecting the selected colony.

    Inputs:    <geometry>  -  Validated colony polygon

    Output:    Iterator of (west, south, east, north) tuples
    '''
    logging.debug('Building bounded map coverage')
    west, south, east, north = geometry.bounds
    stepLon = DEFAULTVIEWPORTLON * 0.75
    stepLat = DEFAULTVIEWPORTLAT * 0.75
    columns = math.ceil((east - west) / stepLon) + 1
    rows = math.ceil((north - south) / stepLat) + 1
    if columns * rows > 2000000:
        raise ValueError('Boundary would create over two million map tiles')
    for rowIndex in range(rows):
        for columnIndex in range(columns):
            tileWest = west + columnIndex * stepLon - DEFAULTVIEWPORTLON * 0.125
            tileSouth = south + rowIndex * stepLat - DEFAULTVIEWPORTLAT * 0.125
            bounds = (tileWest, tileSouth, tileWest + DEFAULTVIEWPORTLON,
                      tileSouth + DEFAULTVIEWPORTLAT)
            if geometry.intersects(box(*bounds)):
                yield bounds


def containsProperty(geometry, propertyInfo):
    '''
    Include property markers within the polygon and on its boundary.

    Inputs:    <geometry>      -  Colony polygon
               <propertyInfo>  -  API property record

    Output:    Boolean indicating whether the marker is in scope
    '''
    logging.debug('Checking property coordinates')
    try:
        lon = float(propertyInfo['Long'])
        lat = float(propertyInfo['Lat'])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(lon) and math.isfinite(lat) and geometry.covers(Point(lon, lat))


def splitBounds(bounds, geometry):
    '''
    Subdivide a dense viewport into overlapping children inside the boundary.

    Inputs:    <bounds>    -  Parent viewport bounds
               <geometry>  -  Colony polygon

    Output:    List of child bounds
    '''
    logging.debug('Splitting dense map viewport')
    west, south, east, north = bounds
    middleLon = (west + east) / 2
    middleLat = (south + north) / 2
    overlapLon = (east - west) * 0.025
    overlapLat = (north - south) * 0.025
    children = [
        (west, south, middleLon + overlapLon, middleLat + overlapLat),
        (middleLon - overlapLon, south, east, middleLat + overlapLat),
        (west, middleLat - overlapLat, middleLon + overlapLon, north),
        (middleLon - overlapLon, middleLat - overlapLat, east, north),
    ]
    return [child for child in children if geometry.intersects(box(*child))]
