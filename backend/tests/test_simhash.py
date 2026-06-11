"""Simhash near-dup detection on synthetic syndicated copies."""

from __future__ import annotations

from connect.knowledge.enrichment.t0 import (
    content_hash,
    from_signed64,
    hamming,
    normalize_text,
    simhash64,
    to_signed64,
)

ARTICLE = """
The Reserve Bank of India on Friday announced an expansion of the digital
rupee pilot programme to twelve more cities, covering retail payments at
fuel stations and metro networks. Officials said the central bank digital
currency has processed over four million transactions since its launch,
with participating banks reporting steady growth in merchant adoption.
The RBI deputy governor told reporters that the pilot would now include
offline payment capability, a feature aimed at users in areas with poor
network connectivity. Industry analysts noted that the move signals the
central bank's intent to scale the project ahead of a wider rollout, while
cautioning that interoperability with the existing unified payments
interface remains the decisive factor for everyday usage. Banks taking
part in the programme will offer incentives to merchants who enable the
new payment rails during the first quarter of the next financial year.
The expansion follows a review meeting between the central bank and the
participating lenders last month, where banks presented data on
transaction failure rates and wallet activation funnels. Several lenders
sought clarity on the liability framework for offline transactions, an
issue the deputy governor said would be addressed in a circular to be
issued before the new phase begins. The central bank also plans a series
of awareness campaigns in the newly added cities, targeting small
merchants and transit commuters, and will publish monthly adoption
statistics on its website for the duration of the pilot. Payment industry
executives expect the programmable money features being tested with state
governments for direct benefit transfers to be the more consequential
track, since success there would demonstrate a use case the existing
unified payments interface cannot replicate today.
"""

# Same wire copy, minor syndication edits (word-level changes).
ARTICLE_EDITED = ARTICLE.replace("on Friday", "on Thursday").replace(
    "twelve more cities", "fifteen more cities").replace(
    "Industry analysts", "Market analysts")

DIFFERENT = """
Parliament's monsoon session opened with a debate on the new data
protection framework, as opposition members pressed the government over
exemptions granted to state agencies. The IT minister defended the bill,
arguing that the carve-outs were narrowly scoped and subject to judicial
review. Civil society groups disagreed, pointing to the broad language of
the national security clause. The bill is expected to be taken up for a
vote next week after the standing committee tabled its report, which
recommended seventeen amendments covering consent, data localisation and
the composition of the regulatory board.
"""


def test_near_duplicate_within_hamming_3():
    a = simhash64(ARTICLE)
    b = simhash64(ARTICLE_EDITED)
    assert hamming(a, b) <= 3


def test_distinct_texts_far_apart():
    a = simhash64(ARTICLE)
    b = simhash64(DIFFERENT)
    assert hamming(a, b) > 10


def test_identical_after_normalization():
    a = simhash64("Hello   world,\n\nthis is\ta test of normalization only")
    b = simhash64("Hello world, this is a test of normalization only")
    assert hamming(a, b) == 0
    assert content_hash("Hello   world  ") == content_hash("Hello world")


def test_normalize_collapses_whitespace_and_nfkc():
    assert normalize_text("a b\n\n  c") == "a b c"


def test_signed64_round_trip():
    for value in (0, 1, 2**63 - 1, 2**63, 2**64 - 1):
        signed = to_signed64(value)
        assert -(2**63) <= signed < 2**63
        assert from_signed64(signed) == value
