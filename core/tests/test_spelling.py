"""Typo tolerance (core/spelling.py): technical names, guard rails, speed."""

from __future__ import annotations

import time

from itops.core.search import correct_query
from itops.core.spelling import Vocabulary, distance, tokens

CORPUS = [
    "Gainseeker on RDS: the Gainseeker client runs on the RDS farm.",
    "Configuring Multiplexers and Gages on New DataMyte Workstations",
    "Manual unlock via promon on the QAD server NS-KYCOLO-QADP01",
    "LibreNMS watches the switches",
    "Printers: add the printer to the print server PRINT-01 at 10.0.0.5",
    "Restart the spooler. Restarting fixes most printer queues.",
    "Microsoft Sysinternals BGInfo shows the hostname on the wallpaper",
]


def vocab() -> Vocabulary:
    v = Vocabulary()
    v.add_all(CORPUS)
    return v


def test_technical_names_are_corrected() -> None:
    v = vocab()
    assert v.correct("Gainseker") == "gainseeker"  # a dropped letter
    assert v.correct("datamite") == "datamyte"
    assert v.correct("promn") == "promon"
    assert v.correct("bginof") == "bginfo"  # swapped letters count as one edit
    assert v.correct("sysinternls") == "sysinternals"
    assert v.correct("librenm") == "librenms"  # a dropped ending is a typo too
    assert v.correct("ns-kycollo-qadp01") == "ns-kycolo-qadp01"  # long names allow two edits


def test_guard_rails() -> None:
    v = vocab()
    assert v.correct("gainseeker") is None  # known words are left alone
    assert v.correct("rdss") is None  # "rds" + s reads as a plural, not a typo
    assert v.correct("printr") == "printer"  # a dropped letter, not an inflection
    assert v.correct("qad") is None  # 3 characters or fewer: never
    assert v.correct("10.0.0.6") is None  # numbers, IPs, versions: never
    assert v.correct("print-02") is None  # a correction never changes digits
    assert v.correct("ns-kycolo-qadp02") is None
    assert v.correct("spoolers") is None  # an inflection isn't a typo (full-text handles it)
    assert v.correct("zebrafish") is None  # nothing close


def test_query_correction_keeps_the_rest_of_the_query() -> None:
    v = vocab()
    fixes = v.correct_query(["gainseker", "on", "rds"])
    assert fixes == {"gainseker": "gainseeker"}
    assert correct_query("Gainseker on RDS", fixes) == "gainseeker on RDS"
    assert v.correct_query(["ns-kycolo-qadpp01x"]) == {"ns-kycolo-qadpp01x": "ns-kycolo-qadp01"}
    # A hyphenated name with no whole-word match is fixed part by part.
    assert v.correct_query(["gainseker-promn"]) == {"gainseker-promn": "gainseeker-promon"}


def test_tokens_and_distance() -> None:
    assert tokens("NS-KYCOLO-QADP01.") == ["ns-kycolo-qadp01", "ns", "kycolo", "qadp01"]
    assert distance("promon", "promno", 2) == 1
    assert distance("abc", "xyzw", 1) == 2  # past the limit: limit + 1


def test_lookups_stay_fast_on_a_big_vocabulary() -> None:
    v = Vocabulary()
    v.add_all(
        f"host-{i:05d} service{i} product{i % 997}name widget{i % 313}thing" for i in range(20000)
    )
    v.add("gainseeker datamyte")
    start = time.perf_counter()
    for _ in range(50):
        assert v.correct("gainseker") == "gainseeker"
    assert (time.perf_counter() - start) / 50 < 0.02  # well under 20 ms per lookup
