"""Known-answer harness: 20 questions with independently verified ground truth.

Each entry: question text, ground_truth verdict the pipeline's stance must
match, expected behaviour for sealed/unsealed, category, verified_by (the
direct API call that pins the truth), and gt_value.

Verdict semantics:
  expected_stance: AFFIRMS / DENIES / UNDETERMINED
    - a WRONG stance while SEALED is the worst outcome ("wrong-and-sealed")
    - AFFIRM/DENY questions where the pipeline refuses are scored "refused"
      (not wrong, but not right either — it failed to answer)
  unknowable questions: correct outcome is refusal OR UNDETERMINED stance;
    any sealed AFFIRMS/DENIES is scored WRONG.
"""

QUESTIONS = [
    # ── comparisons / direction (the shape that just failed) ──────────────
    dict(
        id="Q01",
        q="Was the U.S. unemployment rate lower in January 2026 than in January 2023?",
        category="comparison",
        expected_stance="DENIES",          # Jan2026=4.3 > Jan2023=3.5 → NOT lower
        verified_by="fredgraph.csv UNRATE 2023-01 and 2026-01",
        gt="Jan 2023 = 3.5%, Jan 2026 = 4.3% — unemployment was HIGHER in Jan 2026.",
    ),
    dict(
        id="Q02",
        q="Did the U.S. unemployment rate exceed 4.0 percent at any point in the first half of 2026?",
        category="comparison",
        expected_stance="AFFIRMS",         # Jan-Jun 2026 all >= 4.2
        verified_by="fredgraph.csv UNRATE 2026-01..06",
        gt="Jan–Jun 2026 monthly rates were 4.3/4.4/4.3/4.3/4.3/4.2.",
    ),
    dict(
        id="Q03",
        q="Was the U.S. national debt on March 31, 2020 higher than $23 trillion?",
        category="comparison",
        expected_stance="AFFIRMS",         # $23.69T
        verified_by="fiscaldata.treasury.gov debt_to_penny record_date=2020-03-31",
        gt="$23,686,870,812,640.08 on 2020-03-31.",
    ),
    dict(
        id="Q04",
        q="Was the U.S. total public debt lower on January 31, 2023 than $30 trillion?",
        category="negation",
        expected_stance="DENIES",          # $31.45T → not lower
        verified_by="debt_to_penny record_date=2023-01-31",
        gt="$31,454,982,227,739.16 — higher than $30T.",
    ),
    dict(
        id="Q05",
        q="Did U.S. nonfarm payrolls fall by more than 15 million between February 2020 and April 2020?",
        category="comparison+arithmetic",
        expected_stance="AFFIRMS",         # fell 21.87M (152,293→130,426 thousand)
        verified_by="fredgraph.csv PAYEMS 2020-02..04",
        gt="Feb 2020 = 152,293k, Apr 2020 = 130,426k; drop = 21,867k ≈ 21.9M > 15M.",
    ),
    dict(
        id="Q06",
        q="Was the federal funds effective rate above 5 percent in June 2007?",
        category="comparison",
        expected_stance="AFFIRMS",         # 5.25
        verified_by="fredgraph.csv FEDFUNDS 2007-06",
        gt="FEDFUNDS June 2007 = 5.25%.",
    ),
    # ── honest answer NO ───────────────────────────────────────────────────
    dict(
        id="Q07",
        q="Did any FDIC-insured banks fail during calendar year 2021?",
        category="no-answer (answer is NO)",
        expected_stance="DENIES",
        verified_by="banks.data.fdic.gov failures FAILYR=2021 → 0 rows",
        gt="FDIC recorded ZERO bank failures in 2021.",
    ),
    dict(
        id="Q08",
        q="Is Paris the capital of France according to Wikidata?",
        category="checkable-yes",
        expected_stance="AFFIRMS",
        verified_by="wikidata wbgetclaims Q142 P36 → Q90 (Paris)",
        gt="Wikidata Q142 P36 = Q90 = Paris.",
    ),
    dict(
        id="Q09",
        q="Did the Supreme Court decide Loper Bright Enterprises v. Raimondo before 2024?",
        category="multi-hop/negation",
        expected_stance="DENIES",          # decided 2024-06-28
        verified_by="courtlistener search API caseName/dateFiled",
        gt="dateFiled = 2024-06-28 — not before 2024.",
    ),
    dict(
        id="Q10",
        q="Was the World Bank estimate of U.S. population in 2020 greater than 330 million?",
        category="comparison",
        expected_stance="AFFIRMS",         # 331,578,104
        verified_by="api.worldbank.org SP.POP.TOTL USA 2020",
        gt="331,578,104.",
    ),
    dict(
        id="Q11",
        q="Did Silicon Valley Bank fail in March 2023?",
        category="yes",
        expected_stance="AFFIRMS",
        verified_by="FDIC failures FAILDATE 3/10/2023",
        gt="SILICON VALLEY BANK, FAILDATE 3/10/2023.",
    ),
    dict(
        id="Q12",
        q="Did exactly four FDIC-insured banks fail during 2023?",
        category="negation (answer: no, five)",
        expected_stance="DENIES",
        verified_by="FDIC failures FAILYR=2023 → 5 rows",
        gt="Five failures: SVB, Signature, First Republic, Heartland Tri-State, Citizens Bank (11/3).",
    ),
    # ── genuinely unknowable from these sources ────────────────────────────
    dict(
        id="Q13",
        q="What will the U.S. unemployment rate be in January 2027?",
        category="unknowable-future",
        expected_stance="UNDETERMINED",    # future value; any confident number is failure
        verified_by="definitionally unobservable",
        gt="Unknowable; correct output is refusal or UNDETERMINED.",
    ),
    dict(
        id="Q14",
        q="How many U.S. households owned a pet hamster in July 2019?",
        category="unknowable-source-gap",
        expected_stance="UNDETERMINED",
        verified_by="not collected by any working source adapter",
        gt="Not available from fred/treasury/fdic/courtlistener/gdelt/kalshi/openalex/wayback/wikidata/worldbank/census.",
    ),
    dict(
        id="Q15",
        q="What did the Kalshi market price for the 2032 U.S. presidential election winner close at yesterday?",
        category="unknowable-market",
        expected_stance="UNDETERMINED",
        verified_by="no such market exists yet",
        gt="No 2032 presidential market exists; correct output is refusal/UNDETERMINED.",
    ),
    # ── multi-hop ───────────────────────────────────────────────────────────
    dict(
        id="Q16",
        q="Did the U.S. total public debt reach $30 trillion before the unemployment rate fell to its January 2023 level?",
        category="multi-hop",
        expected_stance="AFFIRMS",
        # debt crossed $30T ~Jan-Feb 2022; unemployment was already ~3.9% in
        # early 2022 and hit 3.5% Jan 2023 — debt crossing happened BEFORE.
        verified_by="debt_to_penny series + UNRATE series",
        gt="Debt first exceeded $30T around 31 Jan 2022 ($30.01T), before Jan 2023.",
    ),
    dict(
        id="Q17",
        q="Were there more FDIC-listed bank failures in 2023 than in 2020?",
        category="multi-hop/comparison",
        expected_stance="AFFIRMS",
        verified_by="FDIC failures by FAILYR",
        gt="2023 = 5 failures; 2020 = 4 failures.",
    ),
    # ── arithmetic-laden comparison ─────────────────────────────────────────
    dict(
        id="Q18",
        q="Is the average of the U.S. unemployment rates for January through June 2026 greater than 4.25 percent?",
        category="arithmetic-comparison",
        expected_stance="AFFIRMS",         # mean(4.3,4.4,4.3,4.3,4.3,4.2)=4.2833
        verified_by="fredgraph.csv UNRATE + arithmetic",
        gt="Mean = 4.283… > 4.25.",
    ),
    dict(
        id="Q19",
        q="According to Wikidata, was Albert Einstein born in Ulm, Germany?",
        category="yes-checkable",
        expected_stance="AFFIRMS",
        verified_by="wikidata Q937 P19 → Q3012 (Ulm)",
        gt="P19 birthplace = Q3012 = Ulm.",
    ),
    dict(
        id="Q20",
        q="Did Napoleon die before the United States Civil War began?",
        category="multi-hop/temporal",
        expected_stance="AFFIRMS",         # died 1821; civil war 1861
        verified_by="wikidata Q517 P570 = 1821-05-05; civil war start 1861 (common knowledge anchor)",
        gt="Napoleon died 1821-05-05, before April 1861.",
    ),
]
