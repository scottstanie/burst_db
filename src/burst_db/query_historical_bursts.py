import csv
import sqlite3
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Callable, Optional, Sequence

import typer
from disp_s1.utils import FRAME_TO_BURST_JSON_FILE, read_zipped_json

DB_PATH = Path("/home/staniewi/dev/coverage-map-s1/global/testing_all_bursts.gpkg")


def _fetch_base(
    frame_ids: list[str],
    row_processor: Callable = lambda row: row,
    select_columns: Sequence[str] = ["*"],
    min_datetime: Optional[datetime] = None,
    max_datetime: Optional[datetime] = None,
    output_file: Optional[typer.FileTextWrite] = None,
    db_path: Path = DB_PATH,
    headers: bool = True,
    debug: bool = False,
) -> list[str]:
    # Step 1: Parse zipped JSON to get the burst_ids for the given frame_ids
    data = read_zipped_json(FRAME_TO_BURST_JSON_FILE)["data"]
    burst_ids = []
    for frame_id in frame_ids:
        burst_ids.extend(data.get(f"{int(frame_id)}", {}).get("burst_ids", []))

    # Step 2: De-duplicate the burst_ids
    unique_burst_ids = list(set(burst_ids))

    # Step 3: Connect to the SQLite database and get the relevant granules
    with sqlite3.connect(db_path) as conn:
        if debug:
            # This adds a callback to print the executed statements to stderr
            conn.set_trace_callback(partial(typer.echo, err=True))

        cursor = conn.cursor()

        query, args = _get_query(
            unique_burst_ids=unique_burst_ids,
            select_columns=select_columns,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
        )
        results = cursor.execute(query, args).fetchall()
        if not output_file:
            output_file = sys.stdout
        writer = csv.writer(output_file)

        if headers:
            writer.writerow([c.replace("DISTINCT ", "") for c in select_columns])
        for row in results:
            granule = row_processor(row)
            writer.writerow([granule])


def fetch_granules(
    frame_ids: list[str],
    min_datetime: Optional[datetime] = None,
    max_datetime: Optional[datetime] = None,
    output_file: Optional[typer.FileTextWrite] = None,
    db_path: Path = DB_PATH,
    headers: bool = False,
    debug: bool = False,
) -> list[str]:
    def row_processor(row):
        return row[0].replace(".SAFE", "")

    return _fetch_base(
        frame_ids=frame_ids,
        row_processor=row_processor,
        min_datetime=min_datetime,
        max_datetime=max_datetime,
        output_file=output_file,
        select_columns=["DISTINCT granule"],
        db_path=db_path,
        headers=headers,
        debug=debug,
    )


def fetch_bursts(
    frame_ids: list[str],
    min_datetime: Optional[datetime] = None,
    max_datetime: Optional[datetime] = None,
    output_file: Optional[typer.FileTextWrite] = None,
    db_path: Path = DB_PATH,
    headers: bool = False,
    with_granule: bool = False,
    debug: bool = False,
) -> list[str]:
    """Get all (burst_id_jpl, sensing_time) for a list of frame ids."""
    select_columns = ["burst_id_jpl", "sensing_time"]
    if with_granule:
        select_columns.append("granule")
    return _fetch_base(
        frame_ids=frame_ids,
        # row_processor=row_processor,
        min_datetime=min_datetime,
        max_datetime=max_datetime,
        output_file=output_file,
        select_columns=select_columns,
        db_path=db_path,
        headers=headers,
        debug=debug,
    )


def _get_query(
    unique_burst_ids: list[str],
    select_columns: Sequence[str],
    min_datetime: Optional[datetime] = None,
    max_datetime: Optional[datetime] = None,
) -> tuple[str, list]:
    """Build up the query to run on the full database."""
    # fid  geom  burst_id_jpl     sensing_time                granule
    query_base = f"""
    FROM bursts
    WHERE bursts.burst_id_jpl IN ({', '.join(['?']*len(unique_burst_ids))})"""

    query = f"SELECT {','.join(select_columns)}" + query_base
    args = unique_burst_ids
    if min_datetime:
        query += "\nAND sensing_time >= ?"
        args += [min_datetime.date()]

    if max_datetime:
        query += "\nAND sensing_time <= ?"
        args += [max_datetime.date()]
    return query, args