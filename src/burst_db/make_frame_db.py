#!/usr/bin/env python
import argparse
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.affinity import translate

ESA_DB_PATH = "burst_map_IW_000001_375887.sqlite3"
ESA_DB_URL = "https://sar-mpc.eu/files/S1_burstid_20220530.zip"

USGS_LAND_FILE = (
    "/Users/staniewi/Documents/Learning/notebooks/usgs_land_1deg_buffered.geojson"
)


def get_esa_burst_db(output_path="esa_burst_map.sqlite3"):
    """Download the ESA burst database and convert to 2D."""
    # Download the ESA burst database

    print("Downloading ESA burst database")
    db_filename = "S1_burstid_20220530/IW/sqlite/burst_map_IW_000001_375887.sqlite3"
    cur_dir = os.getcwd()
    output_path = os.path.abspath(output_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            os.chdir(tmpdir)
            subprocess.check_call(["wget", ESA_DB_URL])

            with zipfile.ZipFile(ESA_DB_URL.split("/")[-1], "r") as zip_ref:
                zip_ref.extract(db_filename)
                shutil.move(db_filename, output_path)
                shutil.rmtree(db_filename.split("/")[0])
        finally:
            os.chdir(cur_dir)


def define_frames_to_burst(df_burst, n_bursts_per_frame=11, overlap=1):
    """Create the JOIN table between frames_number and burst_id."""
    df_burst_count_per_track = (
        df_burst[["relative_orbit_number", "burst_id"]]
        .groupby("relative_orbit_number")
        .burst_id.nunique()
    )
    burst_count_per_track = df_burst_count_per_track.to_dict()

    # frame_to_ogc_fid = {}  # would need to multiply slice idxs by 3
    frame_to_burst_id = []

    for track, count in burst_count_per_track.items():
        current_burst_ids = df_burst[
            df_burst.relative_orbit_number == track
        ].burst_id.unique()
        slices = _make_frame_slices(
            count, n_bursts_per_frame=n_bursts_per_frame, overlap=overlap
        )

        for frame_num, cur_slice in enumerate(slices, start=1):
            for b_id in current_burst_ids[cur_slice]:
                frame_to_burst_id.append((track, frame_num, b_id))

    df_frame_to_burst_id = pd.DataFrame(
        frame_to_burst_id, columns=["relative_orbit_number", "frame_number", "burst_id"]
    )
    return df_frame_to_burst_id


def _make_frame_slices(num_bursts, n_bursts_per_frame=11, overlap=1):
    N = int(np.ceil(num_bursts / (n_bursts_per_frame - overlap)))
    starts = [k * (n_bursts_per_frame - overlap) for k in range(N)]
    return [slice(start, start + n_bursts) for start in starts]


def make_jpl_burst_id(df):
    burst_id_jpl = (
        "t"
        + df["relative_orbit_number"].astype(str).str.zfill(3)
        + "_"
        + df["burst_id"].astype(str).str.zfill(6)
        + "_"
        + df["subswath_name"].str.lower()
    )
    return burst_id_jpl


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
    if y_c >= 84.0:
        return 3413
    elif y_c <= -60.0:
        return 3031

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

    base = 32600 if y_c > 0 else 32700
    # longitude 179 gets 32660 north of the equator
    # longitude -179 gets 32601
    zone_addition = 1 if x_c < 180 else 60
    return base + zone_addition


def get_epsg_codes(df):
    """Get the EPSG codes for all non-antimeridian polygons in a GeoDataFrame.

    Uses the UTM library to account for the oddities of the Zones near Norway.
    """
    import utm  # https://github.com/Turbo87/utm

    epsgs = np.zeros(len(df), dtype=int)

    # do the antimeridian frames first
    am_idxs = df.geometry.map(lambda geo: (geo.geom_type != "Polygon")).values
    epsgs[am_idxs] = df[am_idxs].geometry.map(antimeridian_epsg)

    # everything else
    # get the x, y (lon, lat) coords of all other rows
    other_coords = np.array(
        df[~am_idxs].geometry.map(lambda g: tuple(g.centroid.coords)[0]).tolist()
    )
    xs, ys = other_coords.T
    ys_full_size = np.ones(len(epsgs)) * np.nan
    ys_full_size[~am_idxs] = ys

    idxs = np.logical_and.reduce((~am_idxs, ys_full_size > 84))
    epsgs[idxs] = 3413

    idxs = np.logical_and.reduce((~am_idxs, ys_full_size < -60))
    epsgs[idxs] = 3031

    utm_idxs = np.logical_and(ys < 84, ys > -60)
    north_idxs = ys[utm_idxs] > 0

    # North hemisphere

    zones_north = [
        utm.from_latlon(y, x)[2]
        for (y, x) in zip(ys[utm_idxs][north_idxs], xs[utm_idxs][north_idxs])
    ]
    idxs = np.logical_and.reduce((~am_idxs, ys_full_size < 84, ys_full_size > 0))
    epsgs[idxs] = 32600 + np.array(zones_north)

    # South hemisphere

    zones_south = [
        utm.from_latlon(y, x)[2]
        for (y, x) in zip(ys[utm_idxs][~north_idxs], xs[utm_idxs][~north_idxs])
    ]
    idxs = np.logical_and.reduce((~am_idxs, ys_full_size > -60, ys_full_size < 0))
    epsgs[idxs] = 32700 + np.array(zones_south)
    return epsgs


def make_land_df(
    buffer_deg=1.0, outname="usgs_land_1deg_buffered.geojson", driver="GeoJSON"
):
    if Path(outname).exists():
        return gpd.read_file(outname)
    df_land_cont = gpd.read_file("data/GSHHS_shp/h/GSHHS_h_L1.shp")
    df_antartica = gpd.read_file("data/GSHHS_shp/h/GSHHS_h_L6.shp")
    df_land = pd.concat([df_land_cont, df_antartica], axis=0)[["geometry"]].copy()
    df_land.geometry = df_land.geometry.buffer(buffer_deg)
    df_land = df_land.dissolve()
    if outname:
        df_land.to_file(outname, driver=driver)
    return df_land


def _setup_spatialite_con(con):
    con.enable_load_extension(True)
    # Try the two versions, mac and linux with .so
    try:
        con.load_extension("mod_spatialite")
    except:
        con.load_extension("mod_spatialite.so")
    # Allow us to use spatialite functions in on GPKG files.
    # https://medium.com/@joelmalone/sqlite3-spatialite-and-geopackages-66a08485da6c
    con.execute("SELECT EnableGpkgAmphibiousMode();")


def make_frame_table(outfile):

    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        con.execute(
            "CREATE TABLE frames "
            "(fid INTEGER PRIMARY KEY, relative_orbit_number INTEGER, frame_number INTEGER, epsg INTEGER)"
        )
        # https://groups.google.com/g/spatialite-users/c/XcWvAk7vg0c
        # should add geom after the table is created
        # table_name , geometry_column_name , geometry_type , with_z , with_m , srs_id
        con.execute(
            "SELECT gpkgAddGeometryColumn('frames', 'geom', 'MULTIPOLYGON', 2, 0, 4326);"
        )

        # Aggregates burst geometries for each frame into one
        con.execute(
            """INSERT INTO frames(relative_orbit_number, frame_number, geom)
            SELECT fb.relative_orbit_number,
                    frame_number,
                    ST_UnaryUnion(ST_Collect(geom)) as geom
            FROM burst_id_map b
            JOIN
                frames_bursts fb
                ON b.burst_id = fb.burst_id
            GROUP BY fb.relative_orbit_number, frame_number;
        """
        )
        print("Creating indexes and spatial index...")
        con.execute(
            "CREATE INDEX idx_frames_track_frame ON frames (relative_orbit_number, frame_number)"
        )
        con.execute("SELECT gpkgAddSpatialIndex('frames', 'geom') ;")


def make_frame_to_burst_table(outfile, df_frame_to_burst_id):
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)

        df_frame_to_burst_id.to_sql("frames_bursts", con)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_burst_id_map_burst_id ON burst_id_map (burst_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_bursts_burst_id ON frames_bursts (burst_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_bursts_tracK_frame ON frames_bursts (relative_orbit_number, frame_number)"
        )


def update_burst_epsg(outfile):
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        # add index
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_burst_id_map_burst_id_jpl ON burst_id_map (burst_id_jpl)"
        )
        # Set the EPSG on every burst from the frames
        print("Updating burst EPSGs to match frames...")
        sql = """WITH burst_epsgs AS (
                    SELECT b.OGC_FID,
                        f.epsg
                    FROM burst_id_map b
                    JOIN frames_bursts fb
                    ON b.burst_id = fb.burst_id
                    JOIN frames f
                    ON fb.frame_number = f.frame_number
                    AND fb.relative_orbit_number = f.relative_orbit_number
                )
                UPDATE burst_id_map
                SET epsg = burst_epsgs.epsg
                FROM burst_epsgs
                WHERE burst_id_map.OGC_FID = burst_epsgs.OGC_FID;
        """
        con.execute(sql)

def save_utm_bounding_boxes(outfile, margin=4000, snap=50.0):
    try:
        with sqlite3.connect(outfile) as con:
            con.execute(
                """
            ALTER TABLE burst_id_map ADD xmin INTEGER;
            ALTER TABLE burst_id_map ADD ymin INTEGER;
            ALTER TABLE burst_id_map ADD xmax INTEGER;
            ALTER TABLE burst_id_map ADD ymax INTEGER;
            """
            )
    except:
        # Already exists
        pass

    # Python Snap logic:
    #     xmin -= margin
    #     xmax += margin
    #     ymin -= margin
    #     ymax += margin
    #     xmin = np.round(xmin / snap) * snap
    #     xmax = np.round(xmax / snap) * snap
    #     ymin = np.round(ymin / snap) * snap
    #     ymax = np.round(ymax / snap) * snap

    sql = f"""
    WITH bboxes(b) AS (
        SELECT envelope(transform(geom, epsg))
        FROM burst_id_map
    )
    UPDATE burst_id_map SET (xmin, ymin, xmax, ymax) = (
    SELECT
        Round((ST_MinX(b) - {margin}) / {snap:.1f}) * {snap:.1f},
        Round((ST_MinY(b) - {margin}) / {snap:.1f}) * {snap:.1f},
        Round((ST_MaxX(b) + {margin}) / {snap:.1f}) * {snap:.1f},
        Round((ST_MaxY(b) + {margin}) / {snap:.1f}) * {snap:.1f}
    FROM bboxes
    );
    """
    with sqlite3.connect(outfile) as con:
        _setup_spatialite_con(con)
        con.execute(sql)


def make_minimal_db(db_path, output_path):
    """Make a minimal database with only the burst_id_jpl, epsg, and bbox columns."""
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(
            "SELECT burst_id_jpl, epsg, xmin, ymin, xmax, ymax FROM burst_id_map", con
        )
    # Make sure snapped coordinates as integers (~40% smaller than REAL)
    df["xmin"] = df["xmin"].astype(int)
    df["ymin"] = df["ymin"].astype(int)
    df["xmax"] = df["xmax"].astype(int)
    df["ymax"] = df["ymax"].astype(int)

    with sqlite3.connect(output_path) as con:
        df.to_sql("burst_id_map", con, if_exists="replace", index=False)
        con.execute("CREATE INDEX idx_burst_id_jpl on burst_id_map (burst_id_jpl);")


def get_parser():
    parser = argparse.ArgumentParser(
        description="Generate frames for Sentinel-1 data",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--esa-db-path",
        default="burst_map_IW_000001_375887.sqlite3",
        help=(
            "Path to the ESA sqlite burst database to convert, "
            f"downloaded from {ESA_DB_URL} . Will be downloaded if not exists."
        ),
    )
    parser.add_argument(
        "--output-path",
        type=str,
        default=f"burst_map_IW_000001_375887.OPERA-JPL.{now_str}.sqlite3",
        help="Path to the output database",
    )
    parser.add_argument(
        "--snap",
        type=float,
        default=50.0,
        help="Snap the bounding box to the nearest multiple of this value.",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=4000.0,
        help="Add this margin surrounding the bounding box of bursts.",
    )
    parser.add_argument(
        "-n",
        "--n-bursts-per-frame",
        type=int,
        default=11,
        help="Number of bursts per frame",
    )
    parser.add_argument(
        "--overlap-bursts",
        type=int,
        default=1,
        help="Number of overlapping bursts between frames",
    )
    parser.add_argument(
        "-o",
        "--outfile",
        help="Output file name (default is "
        "'s1-frames-{n_bursts_per_frame}frames-{overlap_bursts}overlap.gpkg'",
    )
    parser.add_argument(
        "--land-buffer-deg",
        type=int,
        default=None,
        help="If provided, a buffer (in degrees) to indicate that a frame is near land.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    t0 = time.time()

    args = get_parser()
    n_bursts = args.n_bursts_per_frame
    overlap = args.overlap_bursts
    if not args.outfile:
        basename = f"s1-frames-{n_bursts}frames-{overlap}overlap"
        outfile = f"{basename}.gpkg"

    # Read ESA's Burst Data
    print("Loading burst data...")
    sql = "SELECT * FROM burst_id_map"
    with sqlite3.connect(ESA_DB_PATH) as con:
        df_burst = gpd.GeoDataFrame.from_postgis(
            sql, con, geom_col="GEOMETRY", crs="EPSG:4326"
        ).rename_geometry("geom")

    print("Forming string JPL id")
    jpl_ids = make_jpl_burst_id(df_burst)
    df_burst.loc[:, "burst_id_jpl"] = jpl_ids
    # placeholder to compute later
    df_burst.loc[:, "epsg"] = 0

    # Start the outfile with the ESA database contents
    print("Saving initial version")
    df_burst.to_file(outfile, driver="GPKG", layer="burst_id_map")

    # TESTING: load what we have
    df_burst = gpd.read_file(outfile, layer="burst_id_map")

    # Create frames
    # Start with the JOIN table
    print("Defining frames")
    df_frame_to_burst_id = define_frames_to_burst(
        df_burst, n_bursts_per_frame=n_bursts, overlap=overlap
    )
    make_frame_to_burst_table(outfile, df_frame_to_burst_id)

    # Then make the frames table
    print("Making frames table by aggregating burst geometries...")
    make_frame_table(outfile)

    # Read in to determine the EPSG codes
    df_frames = gpd.read_file(outfile, layer="frames")

    print("Computing EPSG codes for each frame...")
    epsgs = get_epsg_codes(df_frames)
    df_frames.loc[:, "epsg"] = epsgs

    print("Final number of frames:", len(df_frames))
    print("Saving frames...")
    # weird geopandas thing?
    if "geom" in df_frames.columns and df_frames._geometry_column_name == "geometry":
        df_frames = df_frames.loc[
            :, ["relative_orbit_number", "frame_number", "epsg", "geometry"]
        ]

    df_frames.to_file(outfile, driver="GPKG", layer="frames")

    # # Land Intersection (optional)
    # TODO
    if args.land_buffer_deg is not None:
        print("Indicating which frames are near land...")
        df_land = make_land_df(
            args.land_buffer_deg,
            outname=f"usgs_land_{args.land_buffer_deg}deg_buffered.geojson",
            driver="GeoJSON",
        )
        df_land = make_land_df(args.land_buffer_deg, outname=None)
        land_geo = df_land.geometry.unary_union
        ind_land = df_frames.geometry.intersects(land_geo)
        df_frames.loc[:, "is_near_land"] = ind_land

    update_burst_epsg(outfile)

    # Create the bounding box in UTM coordinates
    save_utm_bounding_boxes(outfile, margin=args.margin, snap=args.snap)

    # Make the minimal version of the DB
    ext = Path(outfile).suffix
    out_minimal = outfile.replace(ext, f"_bbox_only{ext}")
    print(f"Creating a epsg/bbox only version: {out_minimal}")
    make_minimal_db(outfile, out_minimal)

    tf = time.time()
    print(f"Total time: {tf - t0:.2f} seconds")
