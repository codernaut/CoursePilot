"""The eval corpus: small documents written for this project (original text about generic
topics; nothing copied from any course or book). PDFs and PPTX files are generated in code so the
page and slide numbers the questions expect are known exactly."""

from collections.abc import Callable
from dataclasses import dataclass

from tests.docfiles import make_pptx, make_text_pdf


@dataclass(frozen=True)
class EvalDoc:
    title: str
    filename: str
    module: str  # key into MODULES
    build: Callable[[], bytes]
    week: str | None = None


# module key -> (year, block, module) numbers in the seeded programme
MODULES: dict[str, tuple[int, int, int]] = {
    "algorithms": (1, 1, 1),
    "databases": (1, 1, 2),
    "ecology": (1, 2, 1),
}

SORTING_MD = """# Sorting

## Merge sort

Merge sort splits a list into two halves, sorts each half recursively and then merges the two
sorted halves into one. It always takes time proportional to n log n and it needs extra memory
for the merge step. It is a stable sort because equal items keep their original order.

## Quick sort

Quick sort chooses a pivot value and partitions the list so that smaller items come before the
pivot and larger items after it. The two partitions are then sorted recursively. A poor pivot
choice, such as always taking the first item of a sorted list, gives quadratic running time.

## Heap sort

Heap sort first arranges the items into a binary max-heap and then repeatedly removes the
largest item from the root. It sorts in place and never needs more than n log n steps.
"""

INDEXING_MD = """# Database indexes

## B-tree index

A B-tree index keeps keys in sorted order inside wide, shallow nodes, so a lookup needs only a
few page reads even for millions of rows. It supports equality searches, range searches and
ordered scans, which makes it the default index type.

## Hash index

A hash index maps a key through a hash function to a bucket. It answers equality lookups in
roughly constant time but cannot help with range queries or sorting.
"""

NUTRIENTS_MD = """# Nutrient cycles

## The nitrogen cycle

Nitrogen fixing bacteria convert atmospheric nitrogen gas into ammonia that plants can absorb.
Nitrifying bacteria then oxidise ammonia to nitrite and nitrate, and denitrifying bacteria
return nitrogen gas to the air when oxygen is scarce in waterlogged soil.

## The carbon cycle

Plants take up carbon dioxide during photosynthesis and release part of it again through
respiration. Decomposers return the carbon of dead organisms to the air, while burning fossil
fuels adds carbon that was stored underground for millions of years.
"""

SAMPLING_MD = """# Field sampling methods

## Quadrats and transects

A quadrat is a square frame placed at random positions to count plants or slow animals in a
small, known area. A transect is a line laid across a habitat; samples are taken at regular
intervals along it to show how species change with distance, for example from the shore inland.

## Mark and recapture

To estimate the size of a mobile animal population, a first sample is captured, marked and
released. A second sample is captured later. The population estimate is the first sample size
multiplied by the second sample size and divided by the number of marked animals recaptured.
"""


def complexity_pdf() -> bytes:
    return make_text_pdf(
        [
            "Big O notation describes how the running time of an algorithm grows with the input "
            "size n. It ignores constant factors and keeps only the fastest growing term, so "
            "3n squared plus 10n is simply O of n squared.",
            "Amortised analysis averages the cost of a sequence of operations. Appending to a "
            "dynamic array occasionally copies every element, yet the amortised cost of one "
            "append is constant because the capacity doubles each time it is exceeded.",
            "A recurrence relation expresses the cost of a recursive algorithm in terms of "
            "smaller inputs. The master theorem solves recurrences of the form T of n equals a "
            "times T of n over b plus f of n by comparing f of n with n to the log base b of a.",
        ]
    )


def relational_pptx() -> bytes:
    return make_pptx(
        [
            {
                "title": "The relational model",
                "bullets": [
                    "Data is stored in relations, which are tables of rows and columns.",
                    "Every column has a name and a domain of allowed values.",
                    "Relational algebra defines selection, projection and join over relations.",
                ]
                * 2,
            },
            {
                "title": "Keys",
                "bullets": [
                    "A primary key uniquely identifies each row of a table.",
                    "A foreign key refers to the primary key of another table and keeps "
                    "references consistent.",
                ]
                * 3,
            },
            {
                "title": "Normalisation",
                "bullets": [
                    "Normalisation removes redundancy by splitting tables so that every fact "
                    "is stored once.",
                    "Third normal form requires that non-key columns depend only on the key.",
                ]
                * 3,
            },
            {
                "title": "Transactions",
                "bullets": [
                    "A transaction groups several statements into one all-or-nothing unit.",
                    "ACID stands for atomicity, consistency, isolation and durability.",
                ]
                * 3,
                "notes": "Remember that durability means committed data survives a crash.",
            },
        ]
    )


def food_webs_pdf() -> bytes:
    filler = " Energy flows through the ecosystem and only part of it is passed on at each step."
    return make_text_pdf(
        [
            "Producers such as plants and algae make their own food from sunlight. Primary "
            "consumers eat producers and secondary consumers eat primary consumers." + filler,
            "A trophic level is the position of an organism in a food chain. Only about ten "
            "percent of the energy at one trophic level reaches the next, which is why food "
            "chains rarely have more than five levels." + filler,
            "A keystone species has an effect on its community that is far larger than its "
            "numbers suggest. Removing sea otters lets sea urchins graze kelp forests bare."
            + filler,
        ]
    )


def populations_pptx() -> bytes:
    return make_pptx(
        [
            {
                "title": "Exponential growth",
                "bullets": [
                    "With unlimited resources a population grows by a constant fraction "
                    "each generation."
                ]
                * 4,
            },
            {
                "title": "Logistic growth and carrying capacity",
                "bullets": [
                    "Resources are limited, so growth slows as the population approaches the "
                    "carrying capacity of its environment.",
                    "The logistic curve is S shaped and levels off at the carrying capacity.",
                ]
                * 3,
            },
            {
                "title": "Predator and prey cycles",
                "bullets": [
                    "Lynx and snowshoe hare numbers rise and fall in linked cycles, with the "
                    "predator peak lagging behind the prey peak.",
                ]
                * 4,
            },
        ]
    )


DOCS: list[EvalDoc] = [
    EvalDoc(
        "Sorting algorithms", "sorting.md", "algorithms", lambda: SORTING_MD.encode(), "Week 1"
    ),
    EvalDoc("Complexity analysis", "complexity.pdf", "algorithms", complexity_pdf, "Week 2"),
    EvalDoc("Relational databases", "relational.pptx", "databases", relational_pptx, "Week 1"),
    EvalDoc("Database indexes", "indexing.md", "databases", lambda: INDEXING_MD.encode(), "Week 2"),
    EvalDoc("Food webs", "food-webs.pdf", "ecology", food_webs_pdf, "Week 1"),
    EvalDoc("Nutrient cycles", "nutrient-cycles.md", "ecology", lambda: NUTRIENTS_MD.encode()),
    EvalDoc("Population dynamics", "populations.pptx", "ecology", populations_pptx, "Week 3"),
    EvalDoc("Field sampling", "sampling.md", "ecology", lambda: SAMPLING_MD.encode(), "Week 3"),
]
