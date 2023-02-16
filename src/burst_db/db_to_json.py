"""Utilities for convert and reading the minimal burst database to JSON.

The format is a dictionary of burst_id_jpl to a list of 5 elements:

    burst_id -> [epsg, xmin, ymin, xmax, ymax]

In [17]: list(burst_dict.items())[:5]
Out[17]:
[
    ('t001_000001_iw1', ['32631', '532350.0', '79250.0', '629700.0', '127300.0']),
    ('t001_000001_iw2', ['32631', '613150.0', '103650.0', '714350.0', '151350.0']),
    ('t001_000001_iw3', ['32631', '697850.0', '127800.0', '788950.0', '174600.0']),
    ('t001_000002_iw1', ['32631', '528450.0', '97650.0', '625800.0', '145750.0']),
    ('t001_000002_iw2', ['32631', '609300.0', '122100.0', '710450.0', '169800.0'])
]
"""
import gzip
import json

import requests

# import sqlite3
# with sqlite3.connect("burst_map_bbox_only.sqlite3") as con:
#     pd.read_csv("out.csv").to_sql("burst", con, if_exists="replace")
# sqlite3 -header -csv burst_map_bbox_only.sqlite3  \
#     "select burst_id_jpl, epsg, xmin, ymin, xmax, ymax from burst_id_map" \
#     > burst_map_bbox_only.csv

URL = "https://github.com/scottstanie/burst_db/raw/test-json-gz/src/burst_db/data/burst_map_bbox_only.json.gz"

def read_burst_json(fname):
    """Read a pre-downloaded burst-dict file into memory."""
    if str(fname).endswith(".gz"):
        with gzip.open(fname, 'r') as f:
            return json.loads(f.read().decode("utf-8"))
    else:
        with open(fname) as f:
            return json.load(f)


def download_burst_dict(url=URL):
    r = requests.get(url)
    if r.status_code != 200:
        raise ValueError(f"Could not download {url}")
    json_str = gzip.decompress(r.content).decode('utf-8')
    return json.loads(json_str)