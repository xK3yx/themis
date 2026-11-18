"""Pydantic models for ledger nodes, and the node content hash.

`NodeIn` is a discriminated union on `kind`. Models are strict (no type coercion),
frozen, and forbid unknown keys, mirroring the DB's V3 check.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

import pydantic
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from themis.ledger.canonical import content_hash
from themis.ledger.errors import ValidationError

NODE_SCHEMA_VERSION = 1

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Span(_Model):
    """Byte offsets into the exact stored blob, plus the text those bytes decode to."""

    type: Literal["span"] = "span"
    start: Annotated[int, Field(ge=0)]
    end: int
    text: str

    @model_validator(mode="after")
    def _check(self) -> "Span":
        if self.start >= self.end:
            raise ValueError("V5: span start must be < end")
        try:
            n = len(self.text.encode("utf-8"))
        except UnicodeEncodeError as e:
            raise ValueError(f"V6: span text is not valid UTF-8 text: {e}") from None
        if n != self.end - self.start:
            raise ValueError(
                f"V6: span text is {n} bytes in UTF-8 but end - start = {self.end - self.start}"
            )
        return self


class TableCell(_Model):
    """A table cell: the table's span, the cell's own span inside it, and grid coordinates.

    The cell span makes provenance checkable without trusting the parser's grid.
    """

    type: Literal["table_cell"] = "table_cell"
    table: Span
    cell: Span
    row: Annotated[int, Field(ge=0)]
    col: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _cell_inside_table(self) -> "TableCell":
        if not (self.table.start <= self.cell.start and self.cell.end <= self.table.end):
            raise ValueError(
                f"V5b: cell span [{self.cell.start}:{self.cell.end}] is not inside the table "
                f"span [{self.table.start}:{self.table.end}]"
            )
        return self

    @property
    def span(self) -> Span:
        return self.table


Locator = Annotated[Span | TableCell, Field(discriminator="type")]


class _Provenance(_Model):
    supersedes: Hex64 | None = None


class CitedProvenance(_Provenance):
    source: Hex64
    locator: Locator

    @property
    def span(self) -> Span:
        return self.locator if isinstance(self.locator, Span) else self.locator.table


class MeasuredProvenance(_Provenance):
    run: Hex64
    metric: NonEmpty


class DerivedProvenance(_Provenance):
    parents: Annotated[list[Hex64], Field(min_length=2)]
    rule: RuleId

    @model_validator(mode="after")
    def _check(self) -> "DerivedProvenance":
        if len(set(self.parents)) != len(self.parents):
            raise ValueError("V12: derived parents must be distinct")
        if self.supersedes is not None and self.supersedes in self.parents:
            raise ValueError("V15: a node cannot derive from the node it supersedes")
        return self


class PriorProvenance(_Provenance):
    asserted_by: NonEmpty


Content = dict[str, JsonValue]


class CitedNode(_Model):
    kind: Literal["cited"] = "cited"
    content: Content
    provenance: CitedProvenance


class MeasuredNode(_Model):
    kind: Literal["measured"] = "measured"
    content: Content
    provenance: MeasuredProvenance


class DerivedNode(_Model):
    kind: Literal["derived"] = "derived"
    content: Content
    provenance: DerivedProvenance


class PriorNode(_Model):
    kind: Literal["prior"] = "prior"
    content: Content
    provenance: PriorProvenance


NodeIn = Annotated[CitedNode | MeasuredNode | DerivedNode | PriorNode, Field(discriminator="kind")]
_NODE_ADAPTER: pydantic.TypeAdapter = pydantic.TypeAdapter(NodeIn)


def parse_node(data: Any) -> CitedNode | MeasuredNode | DerivedNode | PriorNode:
    """Validate a dict (or pass through a model) as a node; raise the ledger's ValidationError."""
    if isinstance(data, CitedNode | MeasuredNode | DerivedNode | PriorNode):
        return data
    try:
        return _NODE_ADAPTER.validate_python(data)
    except pydantic.ValidationError as e:
        raise ValidationError(f"invalid node: {e}") from None


def provenance_dict(node: Any) -> dict:
    return node.provenance.model_dump(mode="json", exclude_none=True)


def hashable(node: Any, schema_version: int = NODE_SCHEMA_VERSION) -> dict:
    """The exact object whose canonical JSON is hashed into the node id."""
    return {
        "schema_version": schema_version,
        "kind": node.kind,
        "content": node.content,
        "provenance": provenance_dict(node),
    }


def node_id(node: Any, schema_version: int = NODE_SCHEMA_VERSION) -> str:
    return content_hash(hashable(node, schema_version))


@dataclass(frozen=True)
class StoredNode:
    id: str
    created_at: str
    schema_version: int
    node: CitedNode | MeasuredNode | DerivedNode | PriorNode

    @property
    def kind(self) -> str:
        return self.node.kind


@dataclass(frozen=True)
class Observation:
    node_id: str
    producer_sha256: str
    producer: dict
    observed_at: str
