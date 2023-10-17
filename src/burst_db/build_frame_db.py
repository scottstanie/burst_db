#!/usr/bin/env python
import datetime
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import typer
import utm  # https://github.com/Turbo87/utm
from shapely import GeometryType, STRtree
from shapely.affinity import translate
from tqdm.auto import tqdm
from typing_extensions import Annotated

from burst_db import __version__

from . import frames
from ._esa_burst_db import ESA_DB_URL, get_esa_burst_db
from ._land_usgs import GREENLAND_URL, USGS_LAND_URL, get_greenland_shape, get_land_df
from ._opera_north_america import get_opera_na_shape

# Threshold to use EPSG:3413, Sea Ice Polar North (https://epsg.io/3413)
NORTH_THRESHOLD = 75
NORTH_EPSG = 3413
# Threshold to use EPSG:3031, Antarctic Polar Stereographic (https://epsg.io/3031)
SOUTH_THRESHOLD = -60
SOUTH_EPSG = 3031


def make_jpl_burst_id(df: pd.DataFrame):
    """Make the JPL burst ID from the ESA burst ID."""
    burst_id_jpl = (
        "t"
        + df["relative_orbit_number"].astype(str).str.zfill(3)
        + "_"
        + df["burst_id"].astype(str).str.zfill(6)
        + "_"
        + df["subswath_name"].str.lower()
    )
    return burst_id_jpl


def _setup_spatialite_con(con: sqlite3.Connection):
    """Enable spatialite and load the spatialite extension."""
    con.enable_load_extension(True)
    # Try the two versions, mac and linux with .so
    try:
        con.load_extension("mod_spatialite")
    except:
        con.load_extension("mod_spatialite.so")
    # Allow us to use spatialite functions in on GPKG files.
    # https://medium.com/@joelmalone/sqlite3-spatialite-and-geopackages-66a08485da6c
    con.execute("SELECT EnableGpkgAmphibiousMode();")


def make_burst_triplets(df_burst: pd.DataFrame) -> pd.DataFrame:
    """Make a burst triplets dataframe, aggregating IW1,2,3 from the burst dataframe."""

    def join_track_numbers(orbits: list) -> str:
        orbits = list(set(orbits))
        orbits_str = list(map(str, orbits))
        return ",".join(orbits_str)

    df_burst_triplet_temp = df_burst.dissolve(
        by="burst_id",
        aggfunc={
            "OGC_FID": ["min", "max"],
            "relative_orbit_number": join_track_numbers,
            "orbit_pass": "first",
        },
        as_index=False,
    )
    df_burst_triplet = df_burst_triplet_temp.reset_index(drop=True)
    df_burst_triplet.columns = [
        "burst_id",
        "geom",
        "OGC_FID_min",
        "OGC_FID_max",
        "relative_orbit_numbers",
        "look_direction",
    ]
    return df_burst_triplet


def get_land_indicator(gdf: gpd.GeoDataFrame, land_geom: GeometryType.POLYGON):
    """Get a boolean array indicating if each row of `gdf` intersects `land_geom`."""
    tree = STRtree(gdf.geometry)
    idxs_land = tree.query(land_geom, predicate="intersects")
    if idxs_land.ndim == 2:
        idxs_land = idxs_land[1]
    is_in_land = gdf.index.isin(idxs_land)
    return is_in_land


def make_frame_to_burst_table(outfile: str, df_frame_to_burst_id: pd.DataFrame):
    """Create the frames_bursts table and indexes."""
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)

        df_frame_to_burst_id.to_sql("frames_bursts", con, if_exists="replace")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_bursts_burst_ogc_fid ON"
            " frames_bursts (burst_ogc_fid)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_bursts_frame_fid ON frames_bursts"
            " (frame_fid)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_burst_id_map_burst_id ON burst_id_map"
            " (burst_id)"
        )


def make_frame_table(outfile: str):
    """Create the frames table and indexes."""
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        con.execute(
            "CREATE TABLE frames (fid INTEGER PRIMARY KEY, epsg INTEGER, "
            "is_land INTEGER, is_north_america INTEGER)"
        )
        # https://groups.google.com/g/spatialite-users/c/XcWvAk7vg0c
        # should add geom after the table is created
        # table_name , geometry_column_name , geometry_type , with_z , with_m , srs_id
        con.execute(
            "SELECT gpkgAddGeometryColumn('frames', 'geom', 'MULTIPOLYGON', 2, 0,"
            " 4326);"
        )

        # Aggregates burst geometries for each frame into one
        con.execute("""INSERT INTO frames(fid, is_land, geom)
            SELECT fb.frame_fid as fid,
                    fb.is_land,
                    ST_UnaryUnion(ST_Collect(geom)) as geom
            FROM burst_id_map b
            JOIN
                frames_bursts fb
                ON b.ogc_fid = fb.burst_ogc_fid
            GROUP BY 1;
        """)
        print("Creating indexes and spatial index...")
        con.execute("CREATE INDEX IF NOT EXISTS idx_frames_fid ON frames (fid)")
        con.execute("SELECT gpkgAddSpatialIndex('frames', 'geom') ;")
        # Extra thing so that QGIS recognizes "frames" better
        con.execute(
            "UPDATE gpkg_geometry_columns SET geometry_type_name = 'MULTIPOLYGON';"
        )
        # Set the relative_orbit_number as the most common value for each frame
        con.execute("ALTER TABLE frames ADD COLUMN relative_orbit_number INTEGER;")
        con.execute("""WITH frame_tracks AS (
                SELECT f.fid,
                    CAST(ROUND(AVG(b.relative_orbit_number))
                         AS INTEGER) AS relative_orbit_number
                FROM frames f
                JOIN frames_bursts fb ON f.fid = fb.frame_fid
                JOIN burst_id_map b ON fb.burst_ogc_fid = b.ogc_fid
                GROUP BY 1
            ) UPDATE frames SET relative_orbit_number = frame_tracks.relative_orbit_number
            FROM frame_tracks
            WHERE frames.fid = frame_tracks.fid;
            """)
        # Set the orbit_pass as the value from the first burst
        con.execute("ALTER TABLE frames ADD COLUMN orbit_pass TEXT;")
        con.execute("""WITH op AS
                (SELECT f.fid,
                        b.orbit_pass
                FROM frames f
                JOIN frames_bursts fb ON f.fid = fb.frame_fid
                JOIN burst_id_map b ON fb.burst_ogc_fid = b.ogc_fid),
            frame_orbits AS (
                SELECT fid,
                    FIRST_VALUE(orbit_pass) OVER (PARTITION BY fid) AS orbit_pass
                FROM op
                GROUP BY fid)
            UPDATE frames SET orbit_pass = frame_orbits.orbit_pass
            FROM frame_orbits
            WHERE frames.fid = frame_orbits.fid;
            """)

        # Drop the is_land from the frames_bursts table
        con.execute("ALTER TABLE frames_bursts DROP COLUMN is_land;")


def get_epsg_codes(df: gpd.GeoDataFrame):
    """Get the EPSG codes for all non-antimeridian polygons in a GeoDataFrame.

    Uses the UTM library to account for the oddities of the Zones near Norway [1]_.

    References
    ----------
    .. _[1]: http://www.jaworski.ca/utmzones.htm
    """

    def _is_on_antimeridian(geom):
        return geom.geom_type == "MultiPolygon" and len(geom.geoms) > 1

    epsgs = np.zeros(len(df), dtype=int)

    # do the antimeridian frames first
    am_idxs = df.geometry.map(_is_on_antimeridian).values
    epsgs[am_idxs] = df[am_idxs].geometry.map(antimeridian_epsg)

    # everything else
    # get the x, y (lon, lat) coords of all other rows
    other_coords = np.array(
        df[~am_idxs].geometry.map(lambda g: tuple(g.centroid.coords)[0]).tolist()
    )
    xs, ys = other_coords.T
    ys_full_size = np.ones(len(epsgs)) * np.nan
    ys_full_size[~am_idxs] = ys

    idxs = np.logical_and.reduce((~am_idxs, ys_full_size > NORTH_THRESHOLD))
    epsgs[idxs] = NORTH_EPSG

    idxs = np.logical_and.reduce((~am_idxs, ys_full_size < SOUTH_THRESHOLD))
    epsgs[idxs] = SOUTH_EPSG

    utm_idxs = np.logical_and(ys < NORTH_THRESHOLD, ys > SOUTH_THRESHOLD)
    north_idxs = ys[utm_idxs] > 0

    # North hemisphere
    zones_north = [
        utm.from_latlon(y, x)[2]
        for (y, x) in zip(ys[utm_idxs][north_idxs], xs[utm_idxs][north_idxs])
    ]
    idxs = np.logical_and.reduce(
        (~am_idxs, ys_full_size < NORTH_THRESHOLD, ys_full_size > 0)
    )
    epsgs[idxs] = 32600 + np.array(zones_north)

    # South hemisphere
    zones_south = [
        utm.from_latlon(y, x)[2]
        for (y, x) in zip(ys[utm_idxs][~north_idxs], xs[utm_idxs][~north_idxs])
    ]
    idxs = np.logical_and.reduce(
        (~am_idxs, ys_full_size > SOUTH_THRESHOLD, ys_full_size < 0)
    )
    epsgs[idxs] = 32700 + np.array(zones_south)

    # Set all Greenland frames to EPSG:3413
    geom_greenland = get_greenland_shape()
    is_in_greenland = get_land_indicator(df, geom_greenland)
    print(
        f"{is_in_greenland.sum()} frames are in Greenland. Setting to EPSG:{NORTH_EPSG}"
    )
    epsgs[is_in_greenland] = NORTH_EPSG

    return epsgs


def antimeridian_epsg(mp):
    """Calculate the EPSG of multipolygons along the antimeridian.

    Parameters
    ----------
    mp : shapely.geometry.MultiPolygon
        The multipolygon to calculate the EPSG for.

    Returns
    -------
    epsg : int
        The EPSG code for the multipolygon.

    Notes
    -----

    The EPSG code is calculated by taking the weighted average of the centroid of the
    polygons in the multipolygon. The centroid is weighted by the area of the polygon.
    The centroid is shifted by 360 degrees if it is in the western hemisphere.
    """
    y_c = mp.centroid.y
    # check north/south pole cases
    if y_c >= NORTH_THRESHOLD:
        return NORTH_EPSG
    elif y_c <= SOUTH_THRESHOLD:
        return SOUTH_EPSG

    # otherwise, do the weighted average of the shifted polygons to get the centroid
    A = 0
    x_weighted = 0
    # might have 2 or 3 polygons
    for g in mp.geoms:
        A += g.area
        if g.centroid.x < 0:
            g_shifted = translate(g, xoff=360)
            x_weighted += g_shifted.centroid.x * g.area
        else:
            x_weighted += g.centroid.x * g.area
    x_c = x_weighted / A

    # Northern hemisphere = 326XX, southern is 327XX
    base = 32600 if y_c > 0 else 32700
    # EPSG increases negative to positive (west to east)
    # 32601 is at longitude -179 (which 181 after shifting +360)
    zone_addition = 1 if x_c > 180 else 60
    return base + zone_addition


def update_burst_epsg(outfile):
    """Update the EPSG of each burst to match the EPSG of the frame it is in."""
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        # add index
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_burst_id_map_burst_id_jpl ON burst_id_map"
            " (burst_id_jpl)"
        )
        # Set the EPSG on every burst from the frames
        print("Updating burst EPSGs to match frames...")
        sql = """WITH burst_epsgs AS (
                    SELECT b.OGC_FID,
                        f.epsg
                    FROM burst_id_map b
                    JOIN frames_bursts fb
                    ON b.OGC_FID = fb.burst_ogc_fid
                    JOIN frames f
                    ON fb.frame_fid = f.fid
                )
                UPDATE burst_id_map
                SET epsg = burst_epsgs.epsg
                FROM burst_epsgs
                WHERE burst_id_map.OGC_FID = burst_epsgs.OGC_FID;
        """
        con.execute(sql)


def add_gpkg_spatial_ref_sys(outfile):
    """Add all EPSG codes to the gpkg_spatial_ref_sys table."""
    north_hemi_utm = list(range(32601, 32661))
    south_hemi_utm = list(range(32701, 32761))
    epsgs = [SOUTH_EPSG, NORTH_EPSG, 4326] + north_hemi_utm + south_hemi_utm
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        sql = "SELECT gpkgInsertEpsgSRID({epsg});"
        for epsg in tqdm(epsgs):
            try:
                con.execute(sql.format(epsg=epsg))
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                # exists
                pass
        # Fix the gpkg_spatial_ref_sys table for missing UTM zone 32760
        # https://www.gaia-gis.it/fossil/libspatialite/tktview/8b6910dbbb2180026af54a5cc5aac107fb1d62ad?plaintext
        sql = """INSERT INTO gpkg_spatial_ref_sys (
                    srs_name,
                    srs_id,
                    organization,
                    organization_coordsys_id,
                    definition
                    )
                VALUES (
                    'WGS 84 / UTM zone 60S',
                    32760,
                    'EPSG',
                    32760,
'PROJCS["WGS 84 / UTM zone 60S",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",177],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],PARAMETER["false_northing",10000000],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","32760"]]'
                );
        """  # noqa
        try:
            con.execute(sql)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            # exists
            pass
        # More the entries from gpkg_spatial_ref_sys to spatial_ref_sys
        # so we can use the `ST_Transform` function
        con.execute("DROP TABLE IF EXISTS spatial_ref_sys;")
        sql = """
        CREATE TABLE spatial_ref_sys (
            srid       INTEGER NOT NULL PRIMARY KEY,
            auth_name  VARCHAR(256),
            auth_srid  INTEGER,
            srtext     VARCHAR(2048),
            proj4text  VARCHAR(2048)
        );"""
        con.execute(sql)
        sql = """
        INSERT INTO spatial_ref_sys
        SELECT
            srs_id AS srid,
            organization AS auth_name,
            organization_coordsys_id AS auth_srid,
            definition AS srtext,
            NULL
        FROM gpkg_spatial_ref_sys;
        """
        con.execute(sql)


def save_utm_bounding_boxes(outfile, *, margin: float, snap: float):
    """Save the bounding boxes of each burst in UTM coordinates."""
    print("Saving UTM bounding boxes...")
    try:
        with sqlite3.connect(outfile) as con:
            for col in ["xmin", "ymin", "xmax", "ymax"]:
                con.execute(f"ALTER TABLE burst_id_map ADD COLUMN {col} INTEGER;")
    except sqlite3.OperationalError:
        # Already exists
        pass

    sql = f"""
WITH transformed(g, OGC_FID) AS
  (SELECT ST_Envelope(ST_Transform(geom, epsg)) g,
          OGC_FID
   FROM burst_id_map
   WHERE epsg != 0 )
UPDATE burst_id_map
SET (xmin,
     ymin,
     xmax,
     ymax) = (bboxes.xmin,
              bboxes.ymin,
              bboxes.xmax,
              bboxes.ymax)
FROM
  (SELECT OGC_FID,
          FLOOR((ST_MinX(g) - {margin}) / {snap:.1f}) * {snap:.1f} AS xmin,
          FLOOR((ST_MinY(g) - {margin}) / {snap:.1f}) * {snap:.1f} AS ymin,
          CEIL((ST_MaxX(g) + {margin}) / {snap:.1f}) * {snap:.1f} AS xmax,
          CEIL((ST_MaxY(g) + {margin}) / {snap:.1f}) * {snap:.1f} AS ymax
   FROM transformed) AS bboxes
WHERE burst_id_map.OGC_FID = bboxes.OGC_FID ;
    """
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        con.execute(sql)


def make_minimal_db(db_path, df_frame_to_burst_id, output_path):
    """Make a minimal database with only the following columns:

    burst_id_jpl, epsg, xmin, ymin, ymax, ymax, frame_ids
    and a zip file with:
    {burst_id_jpl: (epsg, xmin, ymin, ymax, ymax, frame_ids),...}
    """
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(
            "SELECT OGC_FID, burst_id_jpl, epsg, xmin, ymin, xmax, ymax FROM"
            " burst_id_map",
            con,
        )
    # Make sure snapped coordinates as integers (~40% smaller than REAL)
    df["xmin"] = df["xmin"].astype(int)
    df["ymin"] = df["ymin"].astype(int)
    df["xmax"] = df["xmax"].astype(int)
    df["ymax"] = df["ymax"].astype(int)

    with sqlite3.connect(output_path) as con:
        df.to_sql("burst_id_map", con, if_exists="replace", index=False)

    # Make a version which has the list of frames each burst belongs to
    df_burst_to_frames = _get_burst_to_frame_list(df_frame_to_burst_id)
    df_out = pd.merge(
        df, df_burst_to_frames, how="inner", left_on="OGC_FID", right_on="burst_ogc_fid"
    )
    df_out.drop(columns="OGC_FID", inplace=True)
    return df_out


def make_burst_to_frame_json(df, output_path: str, metadata: dict):
    data_dict = (
        df.set_index("burst_id_jpl")[["frame_fid"]]
        .rename(columns={"frame_fid": "frame_ids"})
        .to_dict(orient="index")
    )
    # Format:  {'t001_000001_iw1': [1], ...}
    dict_out = {"data": data_dict, "metadata": metadata}
    _write_zipped_json(output_path, dict_out)


def make_frame_to_burst_json(db_path: str, output_path: str, metadata: dict):
    with sqlite3.connect(db_path) as con:
        df_frame_to_burst = pd.read_sql_query(
            """
            SELECT
                f.fid AS frame_id,
                f.epsg,
                f.is_land,
                f.is_north_america,
                MIN(xmin) AS xmin,
                MIN(ymin) AS ymin,
                MAX(xmax) AS xmax,
                MAX(ymax) AS ymax,
                GROUP_CONCAT(burst_id_jpl) AS burst_ids
            FROM frames f
            JOIN frames_bursts fb ON fb.frame_fid = f.fid
            JOIN burst_id_map b ON fb.burst_ogc_fid = b.ogc_fid
            GROUP BY 1;
        """,
            con,
        )
    df_frame_to_burst.burst_ids = df_frame_to_burst.burst_ids.str.split(",")
    df_frame_to_burst.is_land = df_frame_to_burst.is_land.astype(bool)
    df_frame_to_burst.is_north_america = df_frame_to_burst.is_north_america.astype(bool)
    data_dict = df_frame_to_burst.set_index("frame_id").to_dict(orient="index")
    dict_out = {"data": data_dict, "metadata": metadata}

    _write_zipped_json(output_path, dict_out)


def _write_zipped_json(json_path, dict_out, level: int = 6):
    json_zip_path = str(json_path) + ".zip"
    with zipfile.ZipFile(
        json_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
    ) as zf:
        zf.writestr(json_path, json.dumps(dict_out))


def _get_burst_to_frame_list(df_frame_to_burst_id):
    """Get a DataFrame which maps the burst ID to the list of frames
    containing the burst.

    Example:
                frame_fid
    burst_ogc_fid
    ...
    24                  [1]
    25               [1, 2]
    26               [1, 2]
    27               [1, 2]
    28                  [2]
    """
    return (
        df_frame_to_burst_id[["burst_ogc_fid", "frame_fid"]]
        .groupby("burst_ogc_fid")
        .agg(list)
    )


def _get_metadata(
    margin, snap, target_frame, land_buffer_deg, optimize_land, min_frame, max_frame
):
    base = {
        "margin": margin,
        "snap": snap,
        "target_frame_size": target_frame,
        "version": __version__,
        "last_modified": datetime.datetime.now().isoformat(),
        "land_buffer_deg": land_buffer_deg,
        "land_optimized": optimize_land,
        "usgs_land_shape_url": USGS_LAND_URL,
        "greenland_shape_url": GREENLAND_URL,
        "esa_burst_db_url": ESA_DB_URL,
    }
    if optimize_land:
        base["min_frame_size"] = min_frame
        base["max_frame_size"] = max_frame
    return base


def create_metadata_table(db_path, metadata):
    """Make the metadata table with the arguments used to create the database."""
    df = pd.DataFrame([metadata])
    with sqlite3.connect(db_path) as con:
        df.to_sql("metadata", con, if_exists="replace", index=False)


def read_zipped_json(filename):
    with zipfile.ZipFile(filename) as zf:
        bytes = zf.read(str(Path(filename).name).replace(".zip", ""))
        return json.loads(bytes.decode())


def create(
    esa_db_path: Annotated[
        str,
        typer.Option(
            help=(
                "Path to the ESA sqlite burst database to convert, downloaded from"
                f" {ESA_DB_URL}. Will be downloaded if not exists."
            ),
        ),
    ] = "burst_map_IW_000001_375887.sqlite3",
    snap: Annotated[
        float,
        typer.Option(
            help="Snap the bounding box to the nearest multiple of this value."
        ),
    ] = 30.0,
    margin: Annotated[
        float,
        typer.Option(help="Add this margin surrounding the bounding box of bursts."),
    ] = 5000.0,
    optimize_land: Annotated[
        bool,
        typer.Option(
            help=(
                "Create frames which attempt to minimize the number of majority-water"
                " frames."
            ),
        ),
    ] = False,
    target_frame: Annotated[
        int, typer.Option(help="Target number of bursts per frame.")
    ] = 9,
    min_frame: Annotated[
        int,
        typer.Option(
            help="(If using `--optimize-land`): Minimum number of bursts per frame."
        ),
    ] = 5,
    max_frame: Annotated[
        int,
        typer.Option(
            help="(If using `--optimize-land`): Maximum number of bursts per frame."
        ),
    ] = 10,
    outfile: Annotated[
        str, typer.Option(help="Output file name")
    ] = "opera-s1-disp.gpkg",
    land_buffer_deg: Annotated[
        float,
        typer.Option(
            help="A buffer (in degrees) to indicate that a frame is near land."
        ),
    ] = 0.3,
):
    """Generate the OPERA frame database for Sentinel-1 data."""
    # Your main processing code would go here
    # For now, just an example of using one of the arguments:

    t0 = time.time()

    # Read ESA's Burst Data
    if not Path(esa_db_path).exists():
        print(f"Downloading {ESA_DB_URL} to {esa_db_path}...")
        get_esa_burst_db(esa_db_path)

    print("Loading burst data...")
    sql = "SELECT * FROM burst_id_map"
    with sqlite3.connect(esa_db_path) as con:
        df_burst = gpd.GeoDataFrame.from_postgis(
            sql, con, geom_col="GEOMETRY", crs="EPSG:4326"
        ).rename_geometry("geom")

    print("Forming string JPL id")
    jpl_ids = make_jpl_burst_id(df_burst)
    df_burst.loc[:, "burst_id_jpl"] = jpl_ids
    # placeholder to compute later
    df_burst.loc[:, "epsg"] = 0

    # Start the outfile with the ESA database contents
    print("Saving initial version of `burst_id_map` table")
    df_burst.set_index("OGC_FID").to_file(
        outfile, driver="GPKG", layer="burst_id_map", index=False
    )
    # Adjust the primary key so it still matches original OGC_FID
    print("Renaming index column from 'fid' to 'OGC_FID'")
    with sqlite3.connect(outfile) as con:
        con.execute("ALTER TABLE burst_id_map RENAME COLUMN fid TO OGC_FID;")

    print("Aggregating burst triplets (grouping IW1,2,3 geometries together)")
    df_burst_triplet = make_burst_triplets(df_burst)

    # Get the land polygon to intersect
    print("Indicating which burst triplets are near land...")
    df_land = get_land_df(land_buffer_deg)
    land_geom = df_land.geometry

    is_in_land = get_land_indicator(df_burst_triplet, land_geom)

    # Create frames and JOIN tables
    # Make the JOIN table first
    print("Defining frames - bursts JOIN table")
    df_frame_to_burst_id = frames.create_frame_to_burst_mapping(
        is_in_land,
        target_frame=target_frame,
        min_frame=min_frame,
        max_frame=max_frame,
        optimize_land=optimize_land,
    )
    make_frame_to_burst_table(outfile, df_frame_to_burst_id)

    # make the "frames" table
    print("Making frames table by aggregating burst geometries...")
    make_frame_table(outfile)
    df_frames = gpd.read_file(outfile, layer="frames")

    print("Computing EPSG codes for each frame...")
    epsgs = get_epsg_codes(df_frames)
    df_frames.loc[:, "epsg"] = epsgs

    # Mark the ones in north america in the OPERA region of interest
    geom_north_america = get_opera_na_shape()
    is_in_north_america = get_land_indicator(df_frames, geom_north_america)
    df_frames.loc[:, "is_north_america"] = is_in_north_america

    print("Final number of frames:", len(df_frames))
    print("Number intersecting land:", is_in_land.sum())
    print("Number in North America:", is_in_north_america.sum())
    print("Saving frames...")
    df_frames.to_file(outfile, driver="GPKG", layer="frames")

    update_burst_epsg(outfile)

    # Create the bounding box in UTM coordinates
    add_gpkg_spatial_ref_sys(outfile)
    save_utm_bounding_boxes(outfile, margin=margin, snap=snap)

    # Make the minimal version of the DB
    ext = Path(outfile).suffix
    out_minimal = outfile.replace(ext, f"-bbox-only{ext}")
    out_burst_to_frame = outfile.replace(ext, "-burst-to-frame.json")
    print(f"Creating a epsg/bbox only version: {out_minimal}")
    df_minimal = make_minimal_db(
        db_path=outfile,
        df_frame_to_burst_id=df_frame_to_burst_id,
        output_path=out_minimal,
    )

    # Get metadata for output dbs
    metadata = _get_metadata(
        margin, snap, target_frame, land_buffer_deg, optimize_land, min_frame, max_frame
    )
    # Create the two JSON mappings:
    # from frame id -> [burst Ids]
    # and burst id -> [frame Ids]
    make_burst_to_frame_json(
        df_minimal, output_path=out_burst_to_frame, metadata=metadata
    )

    out_frame_to_burst = outfile.replace(ext, "-frame-to-burst.json")
    make_frame_to_burst_json(
        db_path=outfile, output_path=out_frame_to_burst, metadata=metadata
    )

    # Add metadata to each
    create_metadata_table(outfile, metadata=metadata)
    create_metadata_table(out_minimal, metadata=metadata)

    print(f"Total time: {time.time() - t0:.2f} seconds")
