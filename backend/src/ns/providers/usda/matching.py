"""Choosing which FoodData Central entry a canonical name refers to. Pure.

The API's relevance score is a text match, and a text match is not an
identity: `chicken breast` scores well against `Chicken breast tenders,
breaded, frozen`, whose nutrition per 100 g is not remotely the same food.
Taking the top hit would attach confident, precise, wrong numbers to a basket
— the exact failure this project exists to avoid.

So candidates are scored here, deliberately and conservatively:

**Both directions matter.** Recall asks how much of what we asked for the
candidate contains; precision asks how much of the candidate is something we
asked for. Recall alone rewards long descriptions that happen to contain our
words among many others.

**Preparation state is a hard constraint, not a term.** Raw, cooked, canned,
frozen, and dried are different foods nutritionally — cooked chicken is denser
in everything per 100 g because water left. A candidate whose state contradicts
the name's state is rejected outright rather than merely scored down.

**Every term we asked for must be present.** Scored against the real payload
for `chicken breast, boneless skinless, raw`, `Chicken, thigh, boneless,
skinless, raw` scores 0.90 — high enough to pass any threshold that admits the
right answer, and wrong. What separates them is that the thigh entry is
missing a word we specified. Canonical names are short and every token in them
is deliberate, so full recall is required for an automatic match.

That is strict, and it is meant to be. `cheese, monterey jack` will not match
USDA's `Cheese, monterey` automatically. The result is a food with no
nutrition, visible in every summary as uncovered mass and fixable in one tap
from the ranked candidates recorded alongside it — which is a better failure
than silently attaching the wrong food's numbers to a year of baskets.
"""

import re
from dataclasses import dataclass

from ns.providers.usda.parsing import ParsedFood

_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")

# Words carrying no distinguishing signal in either vocabulary.
_STOPWORDS = frozenset({"and", "or", "with", "of", "the", "a", "in", "all", "type", "types"})

# Preparation states. Two different ones in the same comparison is a conflict:
# raw and cooked are not the same food per 100 g.
_STATES = frozenset({"raw", "cooked", "canned", "frozen", "dried", "dehydrated", "roasted"})

# States that do not actually conflict with each other. `canned` implies a wet
# pack rather than a cooking method, and USDA often omits `raw` entirely.
_COMPATIBLE = frozenset({frozenset({"canned", "cooked"}), frozenset({"dried", "dehydrated"})})

# How the two directions trade off. Recall is weighted higher because our
# canonical names are short and specific while USDA descriptions are verbose.
RECALL_WEIGHT = 0.7
PRECISION_WEIGHT = 0.3

# Foundation data is analytically measured and the most current; SR Legacy is
# the older standard reference; Survey is modelled for dietary recall studies.
#
# A tiebreaker rather than a bonus added into the score. As a bonus it was
# invisible in the case it exists for: two candidates whose text matches
# perfectly both score 1.0, the clamp swallows the difference, and the older
# record can win on nothing but list order.
_DATA_TYPE_RANK = {"foundation": 2, "sr legacy": 1, "survey (fndds)": 0}

# A floor on the combined score, below which a candidate is not considered
# even at full recall. Leaving a food without nutrition is recoverable;
# attaching the wrong nutrition to a year of baskets is not.
MIN_SCORE = 0.55

# Every token of the canonical name must appear in the candidate. See the
# module docstring: this is what rejects `Chicken, thigh` for a breast query,
# and no score threshold can do it.
MIN_RECALL = 1.0


@dataclass(frozen=True, slots=True)
class Candidate:
    food: ParsedFood
    score: float  # text agreement alone, 0 to 1
    recall: float
    precision: float
    rejected_reason: str | None = None

    @property
    def data_type_rank(self) -> int:
        return _DATA_TYPE_RANK.get((self.food.data_type or "").lower(), 0)

    @property
    def usable(self) -> bool:
        return self.rejected_reason is None and self.score >= MIN_SCORE


def tokenise(text: str) -> set[str]:
    lowered = _PUNCTUATION.sub(" ", text.lower())
    return {word for word in lowered.split() if word and word not in _STOPWORDS}


def _state_conflict(ours: set[str], theirs: set[str]) -> str | None:
    """Whether the two names disagree about preparation state.

    Only a stated disagreement counts. USDA frequently omits `raw` on foods
    that are raw, so a candidate naming no state is not treated as conflicting
    — silence is not a contradiction.
    """
    our_states = ours & _STATES
    their_states = theirs & _STATES
    if not our_states or not their_states or our_states & their_states:
        return None
    if frozenset(our_states | their_states) in _COMPATIBLE:
        return None
    return f"states disagree: {', '.join(sorted(our_states))} vs {', '.join(sorted(their_states))}"


def score_candidate(canonical_name: str, food: ParsedFood) -> Candidate:
    ours = tokenise(canonical_name)
    theirs = tokenise(food.description)
    if not ours or not theirs:
        return Candidate(food=food, score=0.0, recall=0.0, precision=0.0, rejected_reason="empty")

    shared = ours & theirs
    recall = len(shared) / len(ours)
    precision = len(shared) / len(theirs)
    score = RECALL_WEIGHT * recall + PRECISION_WEIGHT * precision

    reason = _state_conflict(ours, theirs)
    if reason is None and recall < MIN_RECALL:
        missing = ", ".join(sorted(ours - theirs))
        reason = f"does not mention {missing}"
    if reason is None and not food.nutrients:
        # A match with no nutrient data is not a match worth making: it would
        # claim the food is known while contributing nothing to any total.
        reason = "candidate carries no nutrient data"

    return Candidate(
        food=food,
        score=round(score, 4),
        recall=round(recall, 4),
        precision=round(precision, 4),
        rejected_reason=reason,
    )


def rank_candidates(canonical_name: str, foods: list[ParsedFood]) -> list[Candidate]:
    """All candidates, best first, rejected ones included.

    Rejected candidates are kept so the stored provenance shows what was
    considered and why it lost — a match that looks wrong later is then
    explainable rather than mysterious.
    """
    scored = [score_candidate(canonical_name, food) for food in foods]
    return sorted(scored, key=lambda c: (c.usable, c.score, c.data_type_rank), reverse=True)


def best_match(canonical_name: str, foods: list[ParsedFood]) -> Candidate | None:
    """The one candidate good enough to use, or None."""
    for candidate in rank_candidates(canonical_name, foods):
        if candidate.usable:
            return candidate
    return None
