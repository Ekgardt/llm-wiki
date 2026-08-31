"""The MemoryAgentBench FactConsolidation stream, parsed without an LLM.

The context of a FactConsolidation row is a flat numbered list of natural
language facts drawn from MQUAKE. The benchmark builds a conflict by
concatenating a fact and its counterfactual rewrite "in order so the
counterfactual appears with a higher serial number" (arXiv 2606.01435), so the
serial is a total order over observations and the highest serial for one
`(subject, property)` key is the value the benchmark expects.

Parsing here is deterministic: a fixed table of sentence frames, each naming
the MQUAKE property it carries. Nothing on this path calls a model, so the
conflict set handed to the two arms of the stand is identical by construction
and the only thing that varies between arms is who resolves the conflict.

A frame that matches nothing is reported, never guessed at. The unparsed count
is part of the result rather than a silent loss.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

FIXTURE_VERSION = "conflict-resolution/v1"
# The row this stand reads. FactConsolidation ships single-hop and multi-hop at
# four context lengths; single-hop at 6K is the shortest row whose questions ask
# directly for a current value, which is the question shape the paper's own
# limitation section says the deterministic rule is for.
DEFAULT_ROW = "factconsolidation_sh_6k"
SOURCE_URL = (
    "https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/"
    "resolve/main/data/Conflict_Resolution-00000-of-00001.parquet"
)

# Each frame names the MQUAKE property it carries. Order matters only where one
# frame is a prefix of another; the specific frames are listed before the loose
# ones so that "was founded in the city of" is never read as "was founded by".
FACT_FRAMES: tuple[tuple[str, str], ...] = (
    (r"^The headquarters of (?P<s>.+) is located in the city of (?P<v>.+)$", "headquarters-city"),
    (r"^The name of the current head of the (?P<s>.+) government is (?P<v>.+)$", "head-of-government"),
    (r"^The name of the current head of state in (?P<s>.+) is (?P<v>.+)$", "head-of-state"),
    (r"^The univeristy where (?P<s>.+) was educated is (?P<v>.+)$", "educated-at"),
    (r"^The chief executive officer of (?P<s>.+) is (?P<v>.+)$", "chief-executive-officer"),
    (r"^The company that produced (?P<s>.+) is (?P<v>.+)$", "producer-company"),
    (r"^The type of music that (?P<s>.+) plays is (?P<v>.+)$", "music-genre"),
    (r"^The official language of (?P<s>.+) is (?P<v>.+)$", "official-language"),
    (r"^The chairperson of (?P<s>.+) is (?P<v>.+)$", "chairperson"),
    (r"^The Prime Minister of (?P<s>.+) is (?P<v>.+)$", "prime-minister"),
    (r"^The director of (?P<s>.+) is (?P<v>.+)$", "director"),
    (r"^The religion of (?P<s>.+) is (?P<v>.+)$", "religion"),
    (r"^The capital of (?P<s>.+) is (?P<v>.+)$", "capital"),
    (r"^The author of (?P<s>.+) is (?P<v>.+)$", "author"),
    (r"^The mother of (?P<s>.+) is (?P<v>.+)$", "mother"),
    (r"^The father of (?P<s>.+) is (?P<v>.+)$", "father"),
    (r"^(?P<s>.+) is affiliated with the religion of (?P<v>.+)$", "religion-affiliation"),
    (r"^(?P<s>.+) is associated with the sport of (?P<v>.+)$", "sport"),
    (r"^(?P<s>.+) is located in the continent of (?P<v>.+)$", "continent"),
    (r"^(?P<s>.+) was created in the country of (?P<v>.+)$", "country-of-origin"),
    (r"^(?P<s>.+) was written in the language of (?P<v>.+)$", "written-language"),
    (r"^(?P<s>.+) was founded in the city of (?P<v>.+)$", "founded-in-city"),
    (r"^(?P<s>.+) was born in the city of (?P<v>.+)$", "birthplace-city"),
    (r"^(?P<s>.+) died in the city of (?P<v>.+)$", "place-of-death-city"),
    (r"^(?P<s>.+) worked in the city of (?P<v>.+)$", "work-location-city"),
    (r"^(?P<s>.+) speaks the language of (?P<v>.+)$", "language-spoken"),
    (r"^(?P<s>.+) plays the position of (?P<v>.+)$", "position-played"),
    (r"^(?P<s>.+) works in the field of (?P<v>.+)$", "occupation"),
    (r"^(?P<s>.+) is a citizen of (?P<v>.+)$", "citizenship"),
    (r"^(?P<s>.+) is married to (?P<v>.+)$", "spouse"),
    (r"^(?P<s>.+) is famous for (?P<v>.+)$", "notable-work"),
    (r"^(?P<s>.+) was performed by (?P<v>.+)$", "performer"),
    (r"^(?P<s>.+) was developed by (?P<v>.+)$", "developer"),
    (r"^(?P<s>.+) was founded by (?P<v>.+)$", "founder"),
    (r"^(?P<s>.+) was created by (?P<v>.+)$", "creator"),
    (r"^(?P<s>.+) is employed by (?P<v>.+)$", "employer"),
    (r"^(?P<s>.+)'s child is (?P<v>.+)$", "child"),
)

_COMPILED = tuple((re.compile(pattern), name) for pattern, name in FACT_FRAMES)
_NUMBERED = re.compile(r"^(?P<serial>\d+)\.[ \t]+(?P<body>.+?)\s*$")


@dataclass(frozen=True)
class ParsedFact:
    """One numbered fact read into a subject, a property and a value."""

    serial: int
    subject: str
    prop: str
    value: str


@dataclass(frozen=True)
class Conflict:
    """One `(subject, property)` key whose value the stream changes at least once."""

    subject: str
    prop: str
    # Ascending by serial. Every observation of this key, including repeats.
    observations: tuple[tuple[int, str], ...]
    # What the benchmark's construction says is current: the highest serial.
    truth: str
    # True when the dataset's own answer set contains the highest-serial value
    # and not the lowest. Those keys carry ground truth the dataset states,
    # rather than ground truth inherited from how the benchmark was built.
    gold_confirmed: bool

    @property
    def key(self) -> str:
        return f"{self.subject}|{self.prop}"

    @property
    def distinct_values(self) -> tuple[str, ...]:
        seen: list[str] = []
        for _, value in self.observations:
            _append_once(seen, value)
        return tuple(seen)


def _append_once(seen: list[str], value: str) -> None:
    if value in seen:
        return
    seen.append(value)


def parse_fact(line: str) -> ParsedFact | None:
    """One numbered line as a fact, or None when no frame claims it."""
    numbered = _NUMBERED.match(line)
    if numbered is None:
        return None
    return _matched_fact(int(numbered["serial"]), numbered["body"].rstrip("."))


def _matched_fact(serial: int, body: str) -> ParsedFact | None:
    for pattern, name in _COMPILED:
        found = pattern.match(body)
        if found is not None:
            return ParsedFact(serial, found["s"].strip(), name, found["v"].strip())
    return None


def parse_context(context: str) -> tuple[tuple[ParsedFact, ...], tuple[str, ...]]:
    """Every fact the frame table claims, and every numbered line it does not."""
    facts: list[ParsedFact] = []
    unparsed: list[str] = []
    for line in context.splitlines():
        _sort_line(line, facts, unparsed)
    return tuple(facts), tuple(unparsed)


def _sort_line(line: str, facts: list[ParsedFact], unparsed: list[str]) -> None:
    if not _NUMBERED.match(line):
        return
    fact = parse_fact(line)
    if fact is None:
        unparsed.append(line.strip())
        return
    facts.append(fact)


def conflicts(facts: Sequence[ParsedFact], gold: Iterable[str]) -> tuple[Conflict, ...]:
    """Every key the stream gives more than one distinct value for."""
    gold_set = frozenset(str(item).strip() for item in gold)
    grouped = _grouped(facts)
    found = [_conflict(key, items, gold_set) for key, items in grouped.items()]
    return tuple(item for item in found if item is not None)


def _grouped(facts: Sequence[ParsedFact]) -> dict[tuple[str, str], list[ParsedFact]]:
    grouped: dict[tuple[str, str], list[ParsedFact]] = {}
    for fact in facts:
        grouped.setdefault((fact.subject, fact.prop), []).append(fact)
    return grouped


def _conflict(
    key: tuple[str, str], items: list[ParsedFact], gold: frozenset[str]
) -> Conflict | None:
    ordered = sorted(items, key=lambda fact: fact.serial)
    observations = tuple((fact.serial, fact.value) for fact in ordered)
    if len({value for _, value in observations}) < 2:
        return None
    truth = observations[-1][1]
    return Conflict(key[0], key[1], observations, truth, _confirmed(observations, gold))


def _confirmed(observations: tuple[tuple[int, str], ...], gold: frozenset[str]) -> bool:
    """Whether the dataset's answers pick the highest serial over the lowest."""
    highest = observations[-1][1] in gold
    lowest = observations[0][1] in gold
    return highest and not lowest


def build_fixture(
    row: Mapping[str, object], *, row_id: str, source_sha256: str
) -> dict[str, object]:
    """The derived conflict set, small enough to keep and to read offline."""
    context = str(row["context"])
    facts, unparsed = parse_context(context)
    gold = _gold_answers(row["answers"])
    found = conflicts(facts, gold)
    return {
        "schema_version": FIXTURE_VERSION,
        "row_id": row_id,
        "source_url": SOURCE_URL,
        "source_sha256": source_sha256,
        "numbered_lines": len(facts) + len(unparsed),
        "parsed_facts": len(facts),
        "unparsed_lines": list(unparsed),
        "conflicts": [_as_json(item) for item in found],
    }


def _gold_answers(answers: object) -> tuple[str, ...]:
    collected: list[str] = []
    for entry in _iterated(answers):
        collected.extend(str(item).strip() for item in _iterated(entry))
    return tuple(collected)


def _iterated(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(value)


def _as_json(item: Conflict) -> dict[str, object]:
    return {
        "subject": item.subject,
        "property": item.prop,
        "observations": [
            {"serial": serial, "value": value} for serial, value in item.observations
        ],
        "truth": item.truth,
        "gold_confirmed": item.gold_confirmed,
    }


def load_fixture(path: Path) -> dict[str, object]:
    """The stored conflict set, refusing a fixture written by another version."""
    document = json.loads(path.read_text(encoding="utf-8"))
    _require_version(document)
    return document


def _require_version(document: object) -> None:
    if not isinstance(document, Mapping):
        raise ValueError("conflict fixture must be a JSON object")
    if document.get("schema_version") != FIXTURE_VERSION:
        raise ValueError(f"conflict fixture is not {FIXTURE_VERSION}")


def fixture_conflicts(document: Mapping[str, object]) -> tuple[Conflict, ...]:
    """The stored conflicts back as records, in the order they were written."""
    stored = document.get("conflicts")
    if not isinstance(stored, Sequence):
        raise ValueError("conflict fixture carries no conflicts array")
    return tuple(_from_json(item) for item in stored)


def _from_json(item: object) -> Conflict:
    if not isinstance(item, Mapping):
        raise ValueError("a stored conflict must be an object")
    observations = tuple(
        (int(entry["serial"]), str(entry["value"]))
        for entry in _iterated(item["observations"])
        if isinstance(entry, Mapping)
    )
    return Conflict(
        str(item["subject"]),
        str(item["property"]),
        observations,
        str(item["truth"]),
        bool(item["gold_confirmed"]),
    )
