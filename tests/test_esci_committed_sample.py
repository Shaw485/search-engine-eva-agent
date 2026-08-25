from __future__ import annotations

from pathlib import Path

import polars as pl

from search_quality.data.contracts import sha256_file

SAMPLE = Path(__file__).parents[1] / "data" / "samples" / "esci-stage1-smoke.parquet"


def test_committed_esci_sample_matches_stage1_evidence() -> None:
    frame = pl.read_parquet(SAMPLE)
    assert frame.height == 416
    assert frame.get_column("query_id").n_unique() == 20
    assert set(frame.get_column("esci_label").unique()) <= {"E", "S", "C", "I"}
    assert frame.get_column("eval_split").unique().to_list() == ["dev"]
    assert frame.get_column("is_smoke").all()
    assert sha256_file(SAMPLE) == (
        "b0512f03c0a11ff443f3dab4336c9780e75d8f0b18f1cc30c047ef777603a9a7"
    )
