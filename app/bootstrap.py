# -*- coding: utf-8 -*-
"""容器启动引导：建库；空库且 LOAD_SAMPLE_ON_EMPTY=1 时载入样例。"""

import os

from .sample_data import SAMPLE_SHEETS, SAMPLE_SOUNDINGS_CSV
from .sections import parse_sounding_csv
from .storage import Store


def main() -> None:
    db_path = os.environ.get("DATUM_DB", "/data/datum.db")
    store = Store(
        db_path,
        datum=os.environ.get("DATUM_POINT", "D0"),
        span_limit=int(os.environ.get("SPAN_LIMIT", "20")),
    )
    load_sample = os.environ.get("LOAD_SAMPLE_ON_EMPTY") == "1"
    if load_sample and not store.list_sheets():
        store.replace_all(SAMPLE_SHEETS)
        print("bootstrap: sample sheets loaded, %d sheets" % len(SAMPLE_SHEETS))
    else:
        print("bootstrap: %d existing sheets, no sample sheets loaded"
              % len(store.list_sheets()))

    if load_sample and not store.list_soundings():
        soundings = parse_sounding_csv(SAMPLE_SOUNDINGS_CSV)
        store.replace_soundings(soundings, csv_text=SAMPLE_SOUNDINGS_CSV)
        print("bootstrap: sample soundings loaded, %d rows" % len(soundings))
    else:
        print("bootstrap: %d existing sounding rows"
              % len(store.list_soundings()))
    store.close()


if __name__ == "__main__":
    main()
