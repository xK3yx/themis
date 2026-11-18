"""Golden fixtures: licences, byte spans and coverage (parser-independent), then the parser
against every golden, case by case and in full (M1 spec §10.2)."""

import pytest

from tests.extract.golden import CASES, ROOT, byte_problems, licence_problems, load_goldens

GOLDENS = load_goldens()
IDS = [g.id for g in GOLDENS]


# ---------------------------------------------------------------- parser-independent


def test_goldens_exist():
    assert GOLDENS, f"no golden fixtures under {ROOT}"


@pytest.mark.parametrize("d", sorted({g.dir for g in GOLDENS}), ids=lambda d: d.name)
def test_licence(d):
    import json

    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert licence_problems(meta, d) == []


@pytest.mark.parametrize("g", GOLDENS, ids=IDS)
def test_byte_spans(g):
    assert byte_problems(g) == []


def test_coverage_and_real_majority():
    covered_real = {c for g in GOLDENS if not g.synthetic for c in g.cases}
    covered_any = {c for g in GOLDENS for c in g.cases}
    assert set(CASES) <= covered_any, f"uncovered cases: {sorted(set(CASES) - covered_any)}"
    real = sum(1 for g in GOLDENS if not g.synthetic)
    synthetic = len(GOLDENS) - real
    assert real > synthetic, f"real {real} must outnumber synthetic {synthetic}"
    real_papers = {g.meta["arxiv_id"] for g in GOLDENS if not g.synthetic}
    assert len(real_papers) >= 10, f"only {len(real_papers)} real papers"
    # Synthetic tables may only cover cases no real table covers.
    for g in GOLDENS:
        if g.synthetic:
            extra = set(g.cases) & covered_real
            assert not extra, f"{g.id} is synthetic but covers {sorted(extra)}, already real"


@pytest.mark.parametrize("g", GOLDENS, ids=IDS)
def test_cases_are_known(g):
    assert g.cases and set(g.cases) <= set(CASES)


def test_byte_check_catches_a_wrong_hand_written_value():
    """The mechanical check is not a tautology: a mistyped value or literal is caught."""
    import copy

    from tests.extract.golden import byte_problems, expand

    g = next(g for g in GOLDENS if g.name == "quant_methods")
    for mutate in (
        lambda cell: cell.__setitem__("num", 324.9),  # value doesn't match the bytes
        lambda cell: cell.__setitem__("lit", "324.9"),  # literal not in the cell
    ):
        exp = copy.deepcopy(g.expected)
        mutate(exp["tables"][0]["rows"][2][2])
        g2 = copy.copy(g)
        g2.tables = [expand(g.tex, t) for t in exp["tables"]]
        assert byte_problems(g2), "a wrong value must be reported"
    exp = copy.deepcopy(g.expected)
    exp["tables"][0]["rows"][2][1] = "NF5"  # raw text that isn't in the source
    assert expand(g.tex, exp["tables"][0]).problems
