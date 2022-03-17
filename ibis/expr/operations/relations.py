from __future__ import annotations

import collections
import itertools
from typing import Hashable, MutableMapping

from cached_property import cached_property
from public import public

from ... import util
from ...common import exceptions as com
from .. import rules as rlz
from .. import schema as sch
from .. import types as ir
from .core import Node, distinct_roots
from .sortkeys import _maybe_convert_sort_keys

_table_names = (f'unbound_table_{i:d}' for i in itertools.count())


@public
def genname():
    return next(_table_names)


@public
class TableNode(Node):
    def get_type(self, name):
        return self.schema[name]

    def output_type(self):
        return ir.TableExpr

    def aggregate(self, this, metrics, by=None, having=None):
        return Aggregation(this, metrics, by=by, having=having)

    def sort_by(self, expr, sort_exprs):
        return Selection(
            expr,
            [],
            sort_keys=_maybe_convert_sort_keys(
                [self.to_expr(), expr],
                sort_exprs,
            ),
        )

    def is_ancestor(self, other):
        import ibis.expr.lineage as lin

        if isinstance(other, ir.Expr):
            other = other.op()

        if self.equals(other):
            return True

        fn = lambda e: (lin.proceed, e.op())  # noqa: E731
        expr = self.to_expr()
        for child in lin.traverse(fn, expr):
            if child.equals(other):
                return True
        return False


@public
class PhysicalTable(TableNode, sch.HasSchema):
    def blocks(self):
        return True


@public
class UnboundTable(PhysicalTable):
    schema = rlz.instance_of(sch.Schema)
    name = rlz.optional(rlz.instance_of(str), default=genname)

    def __component_eq__(
        self,
        other: UnboundTable,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return self.name == other.name and self.schema.equals(
            other.schema, cache=cache
        )


@public
class DatabaseTable(PhysicalTable):
    name = rlz.instance_of(str)
    schema = rlz.instance_of(sch.Schema)
    source = rlz.client

    def change_name(self, new_name):
        return type(self)(new_name, self.args[1], self.source)

    def __component_eq__(
        self,
        other: DatabaseTable,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            self.name == other.name
            and self.source == other.source
            and self.schema.equals(other.schema, cache=cache)
        )


@public
class SQLQueryResult(TableNode, sch.HasSchema):
    """A table sourced from the result set of a select query"""

    query = rlz.instance_of(str)
    schema = rlz.instance_of(sch.Schema)
    source = rlz.client

    def blocks(self):
        return True

    def __component_eq__(
        self,
        other: SQLQueryResult,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            self.query == other.query
            and self.schema.equals(other.schema, cache=cache)
            and self.source == other.source
        )


def _make_distinct_join_predicates(left, right, predicates):
    # see GH #667

    # If left and right table have a common parent expression (e.g. they
    # have different filters), must add a self-reference and make the
    # appropriate substitution in the join predicates

    if left.equals(right):
        right = right.view()

    predicates = _clean_join_predicates(left, right, predicates)
    return left, right, predicates


def _clean_join_predicates(left, right, predicates):
    import ibis.expr.analysis as L

    result = []

    if not isinstance(predicates, (list, tuple)):
        predicates = [predicates]

    for pred in predicates:
        if isinstance(pred, tuple):
            if len(pred) != 2:
                raise com.ExpressionError('Join key tuple must be ' 'length 2')
            lk, rk = pred
            lk = left._ensure_expr(lk)
            rk = right._ensure_expr(rk)
            pred = lk == rk
        elif isinstance(pred, str):
            pred = left[pred] == right[pred]
        elif not isinstance(pred, ir.Expr):
            raise NotImplementedError

        if not isinstance(pred, ir.BooleanColumn):
            raise com.ExpressionError('Join predicate must be comparison')

        preds = L.flatten_predicate(pred)
        result.extend(preds)

    _validate_join_predicates(left, right, result)
    return tuple(result)


def _validate_join_predicates(left, right, predicates):
    from ibis.expr.analysis import fully_originate_from

    # Validate join predicates. Each predicate must be valid jointly when
    # considering the roots of each input table
    for predicate in predicates:
        if not fully_originate_from(predicate, [left, right]):
            raise com.RelationError(
                'The expression {!r} does not fully '
                'originate from dependencies of the table '
                'expression.'.format(predicate)
            )


@public
class Join(TableNode):
    left = rlz.table
    right = rlz.table
    # TODO(kszucs): convert to proper predicate rules
    predicates = rlz.optional(lambda x, this: x, default=())

    def __init__(self, left, right, predicates, **kwargs):
        left, right, predicates = _make_distinct_join_predicates(
            left, right, predicates
        )
        super().__init__(
            left=left, right=right, predicates=predicates, **kwargs
        )

    @property
    def schema(self):
        # For joins retaining both table schemas, merge them together here
        return self.left.schema().append(self.right.schema())

    def has_schema(self):
        return not set(self.left.columns) & set(self.right.columns)

    def root_tables(self):
        if util.all_of([self.left.op(), self.right.op()], (Join, Selection)):
            # Unraveling is not possible
            return [self.left.op(), self.right.op()]
        else:
            return distinct_roots(self.left, self.right)

    def __component_eq__(
        self,
        other: Join,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            util.seq_eq(self.predicates, other.predicates, cache=cache)
            and self.left.equals(other.left, cache=cache)
            and self.right.equals(other.right, cache=cache)
        )


@public
class InnerJoin(Join):
    pass


@public
class LeftJoin(Join):
    pass


@public
class RightJoin(Join):
    pass


@public
class OuterJoin(Join):
    pass


@public
class AnyInnerJoin(Join):
    pass


@public
class AnyLeftJoin(Join):
    pass


@public
class LeftSemiJoin(Join):
    @property
    def schema(self):
        return self.left.schema()


@public
class LeftAntiJoin(Join):
    @property
    def schema(self):
        return self.left.schema()


@public
class CrossJoin(Join):
    def __component_eq__(
        self,
        other: CrossJoin,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return self.left.equals(other.left, cache=cache) and self.right.equals(
            other.right,
            cache=cache,
        )


@public
class AsOfJoin(Join):
    # TODO(kszucs): convert to proper predicate rules
    by = rlz.optional(lambda x, this: x, default=())
    tolerance = rlz.optional(rlz.interval)

    def __init__(self, left, right, predicates, by, tolerance):
        by = _clean_join_predicates(left, right, by)
        super().__init__(
            left=left,
            right=right,
            predicates=predicates,
            by=by,
            tolerance=tolerance,
        )

    def __component_eq__(
        self,
        other: AsOfJoin,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            (
                (self.tolerance is None and other.tolerance is None)
                or self.tolerance.equals(other.tolerance, cache=cache)
            )
            and util.seq_eq(self.by, other.by, cache=cache)
            and super().__component_eq__(other, cache=cache)
        )


@public
class SetOp(TableNode, sch.HasSchema):
    left = rlz.table
    right = rlz.table

    def __init__(self, left, right, **kwargs):
        if not left.schema().equals(right.schema()):
            raise com.RelationError(
                'Table schemas must be equal for set operations'
            )
        super().__init__(left=left, right=right, **kwargs)

    @property
    def schema(self):
        return self.left.schema()

    def blocks(self):
        return True

    def __component_eq__(
        self,
        other: SetOp,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return self.left.equals(other.left, cache=cache) and self.right.equals(
            other.right,
            cache=cache,
        )


@public
class Union(SetOp):
    distinct = rlz.optional(rlz.instance_of(bool), default=False)


@public
class Intersection(SetOp):
    pass


@public
class Difference(SetOp):
    pass


@public
class Limit(TableNode):
    table = rlz.table
    n = rlz.instance_of(int)
    offset = rlz.instance_of(int)

    def blocks(self):
        return True

    @property
    def schema(self):
        return self.table.schema()

    def has_schema(self):
        return self.table.op().has_schema()

    def root_tables(self):
        return [self]

    def __component_eq__(
        self,
        other: Limit,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            self.n == other.n
            and self.offset == other.offset
            and self.table.equals(other.table, cache=cache)
        )


@public
class SelfReference(TableNode, sch.HasSchema):
    table = rlz.table

    @property
    def schema(self):
        return self.table.schema()

    def root_tables(self):
        # The dependencies of this operation are not walked, which makes the
        # table expression holding this relationally distinct from other
        # expressions, so things like self-joins are possible
        return [self]

    def blocks(self):
        return True

    def __component_eq__(
        self,
        other: SelfReference,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return self.table.equals(other.table, cache=cache)


@public
class Selection(TableNode, sch.HasSchema):
    table = rlz.table
    selections = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.table,
                    rlz.column_from("table"),
                    rlz.function_of("table"),
                    rlz.any,
                    rlz.named_literal,
                )
            )
        ),
        default=(),
    )
    predicates = rlz.optional(rlz.list_of(rlz.boolean), default=())
    sort_keys = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.column_from("table"),
                    rlz.function_of("table"),
                    rlz.sort_key(from_="table"),
                    rlz.pair(
                        rlz.one_of(
                            (
                                rlz.column_from("table"),
                                rlz.function_of("table"),
                                rlz.any,
                            )
                        ),
                        rlz.map_to(
                            {
                                True: True,
                                False: False,
                                "desc": False,
                                "descending": False,
                                "asc": True,
                                "ascending": True,
                                1: True,
                                0: False,
                            }
                        ),
                    ),
                )
            )
        ),
        default=(),
    )

    def __init__(self, table, selections, predicates, sort_keys):
        from ibis.expr.analysis import FilterValidator

        # Need to validate that the column expressions are compatible with the
        # input table; this means they must either be scalar expressions or
        # array expressions originating from the same root table expression
        dependent_exprs = selections + sort_keys
        table._assert_valid(dependent_exprs)

        # Validate predicates
        validator = FilterValidator([table])
        validator.validate_all(predicates)

        super().__init__(
            table=table,
            selections=selections,
            predicates=predicates,
            sort_keys=sort_keys,
        )

        # Validate no overlapping columns in schema
        assert self.schema

    def __component_eq__(
        self,
        other: Selection,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            util.seq_eq(self.selections, other.selections, cache=cache)
            and util.seq_eq(self.predicates, other.predicates, cache=cache)
            and util.seq_eq(self.sort_keys, other.sort_keys, cache=cache)
            and self.table.equals(other.table, cache=cache)
        )

    @cached_property
    def schema(self):
        # Resolve schema and initialize
        if not self.selections:
            return self.table.schema()

        types = []
        names = []

        for projection in self.selections:
            if isinstance(projection, ir.DestructColumn):
                # If this is a destruct, then we destructure
                # the result and assign to multiple columns
                struct_type = projection.type()
                for name in struct_type.names:
                    names.append(name)
                    types.append(struct_type[name])
            elif isinstance(projection, ir.ValueExpr):
                names.append(projection.get_name())
                types.append(projection.type())
            elif isinstance(projection, ir.TableExpr):
                schema = projection.schema()
                names.extend(schema.names)
                types.extend(schema.types)

        return sch.Schema(names, types)

    def blocks(self):
        return bool(self.selections)

    def substitute_table(self, table_expr):
        return Selection(table_expr, self.selections)

    def root_tables(self):
        return [self]

    def can_add_filters(self, wrapped_expr, predicates):
        pass

    def empty_or_equal(self, other, cache) -> bool:
        for field in "selections", "sort_keys", "predicates":
            selfs = getattr(self, field)
            others = getattr(other, field)
            valid = (
                not selfs
                or not others
                or util.seq_eq(selfs, others, cache=cache)
            )
            if not valid:
                return False
        return True

    def compatible_with(self, other, cache=None):
        if cache is None:
            cache = {}
        # self and other are equivalent except for predicates, selections, or
        # sort keys any of which is allowed to be empty. If both are not empty
        # then they must be equal
        if self.equals(other, cache=cache):
            return True

        if not isinstance(other, type(self)):
            return False

        return self.table.equals(other.table, cache=cache) and (
            self.empty_or_equal(other, cache=cache)
        )

    # Operator combination / fusion logic

    def aggregate(self, this, metrics, by=None, having=None):
        if len(self.selections) > 0:
            return Aggregation(this, metrics, by=by, having=having)
        else:
            helper = AggregateSelection(this, metrics, by, having)
            return helper.get_result()

    def sort_by(self, expr, sort_exprs):
        resolved_keys = _maybe_convert_sort_keys(
            [self.table, expr], sort_exprs
        )
        if not self.blocks():
            if self.table._is_valid(resolved_keys):
                return Selection(
                    self.table,
                    self.selections,
                    predicates=self.predicates,
                    sort_keys=self.sort_keys + resolved_keys,
                )

        return Selection(expr, [], sort_keys=resolved_keys)


@public
class AggregateSelection:
    # sort keys cannot be discarded because of order-dependent
    # aggregate functions like GROUP_CONCAT

    def __init__(self, parent, metrics, by, having):
        self.parent = parent
        self.op = parent.op()
        self.metrics = metrics
        self.by = by
        self.having = having

    def get_result(self):
        if self.op.blocks():
            return self._plain_subquery()
        else:
            return self._attempt_pushdown()

    def _plain_subquery(self):
        return Aggregation(
            self.parent, self.metrics, by=self.by, having=self.having
        )

    def _attempt_pushdown(self):
        metrics_valid, lowered_metrics = self._pushdown_exprs(self.metrics)
        by_valid, lowered_by = self._pushdown_exprs(self.by)
        having_valid, lowered_having = self._pushdown_exprs(self.having)

        if metrics_valid and by_valid and having_valid:
            return Aggregation(
                self.op.table,
                lowered_metrics,
                by=lowered_by,
                having=lowered_having,
                predicates=self.op.predicates,
                sort_keys=self.op.sort_keys,
            )
        else:
            return self._plain_subquery()

    def _pushdown_exprs(self, exprs):
        import ibis.expr.analysis as L

        # exit early if there's nothing to push down
        if not exprs:
            return True, []

        resolved = self.op.table._resolve(exprs)
        subbed_exprs = []

        valid = False
        if resolved:
            for x in util.promote_list(resolved):
                subbed = L.sub_for(x, [(self.parent, self.op.table)])
                subbed_exprs.append(subbed)
            valid = self.op.table._is_valid(subbed_exprs)
        else:
            valid = False

        return valid, subbed_exprs


@public
class Aggregation(TableNode, sch.HasSchema):

    """
    metrics : per-group scalar aggregates
    by : group expressions
    having : post-aggregation predicate

    TODO: not putting this in the aggregate operation yet
    where : pre-aggregation predicate
    """

    table = rlz.table
    metrics = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.function_of(
                        "table",
                        output_rule=rlz.one_of(
                            (rlz.reduction, rlz.scalar(rlz.any))
                        ),
                    ),
                    rlz.reduction,
                    rlz.scalar(rlz.any),
                    rlz.list_of(rlz.scalar(rlz.any)),
                    rlz.named_literal,
                )
            ),
            flatten=True,
        ),
        default=(),
    )
    by = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.function_of("table"),
                    rlz.column_from("table"),
                    rlz.column(rlz.any),
                )
            )
        ),
        default=(),
    )
    having = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.function_of(
                        "table", output_rule=rlz.scalar(rlz.boolean)
                    ),
                    rlz.scalar(rlz.boolean),
                )
            ),
        ),
        default=(),
    )
    predicates = rlz.optional(rlz.list_of(rlz.boolean), default=())
    sort_keys = rlz.optional(
        rlz.list_of(
            rlz.one_of(
                (
                    rlz.column_from("table"),
                    rlz.function_of("table"),
                    rlz.sort_key(from_="table"),
                    rlz.pair(
                        rlz.one_of(
                            (
                                rlz.column_from("table"),
                                rlz.function_of("table"),
                                rlz.any,
                            )
                        ),
                        rlz.map_to(
                            {
                                True: True,
                                False: False,
                                "desc": False,
                                "descending": False,
                                "asc": True,
                                "ascending": True,
                                1: True,
                                0: False,
                            }
                        ),
                    ),
                )
            )
        ),
        default=(),
    )

    def __init__(self, table, metrics, by, having, predicates, sort_keys):
        from ibis.expr.analysis import FilterValidator

        # All non-scalar refs originate from the input table
        all_exprs = metrics + by + having + sort_keys
        table._assert_valid(all_exprs)

        # Validate predicates
        validator = FilterValidator([table])
        validator.validate_all(predicates)

        if not by:
            sort_keys = tuple()

        super().__init__(
            table=table,
            metrics=metrics,
            by=by,
            having=having,
            predicates=predicates,
            sort_keys=sort_keys,
        )
        # Validate schema has no overlapping columns
        assert self.schema

    def __component_eq__(
        self,
        other: Aggregation,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            util.seq_eq(self.metrics, other.metrics, cache=cache)
            and util.seq_eq(self.by, other.by, cache=cache)
            and util.seq_eq(self.having, other.having, cache=cache)
            and util.seq_eq(self.predicates, other.predicates, cache=cache)
            and util.seq_eq(self.sort_keys, other.sort_keys, cache=cache)
            and self.table.equals(other.table, cache=cache)
        )

    def blocks(self):
        return True

    def substitute_table(self, table_expr):
        return Aggregation(
            table_expr, self.metrics, by=self.by, having=self.having
        )

    @cached_property
    def schema(self):
        names = []
        types = []

        for e in self.by + self.metrics:
            if isinstance(e, ir.DestructValue):
                # If this is a destruct, then we destructure
                # the result and assign to multiple columns
                struct_type = e.type()
                for name in struct_type.names:
                    names.append(name)
                    types.append(struct_type[name])
            else:
                names.append(e.get_name())
                types.append(e.type())

        return sch.Schema(names, types)

    def sort_by(self, expr, sort_exprs):
        resolved_keys = _maybe_convert_sort_keys(
            [self.table, expr], sort_exprs
        )
        if self.table._is_valid(resolved_keys):
            return Aggregation(
                self.table,
                self.metrics,
                by=self.by,
                having=self.having,
                predicates=self.predicates,
                sort_keys=self.sort_keys + resolved_keys,
            )

        return Selection(expr, [], sort_keys=resolved_keys)


@public
class Distinct(TableNode, sch.HasSchema):
    """
    Distinct is a table-level unique-ing operation.

    In SQL, you might have:

    SELECT DISTINCT foo
    FROM table

    SELECT DISTINCT foo, bar
    FROM table
    """

    table = rlz.table

    def __init__(self, table):
        # check whether schema has overlapping columns or not
        assert table.schema()
        super().__init__(table=table)

    @property
    def schema(self):
        return self.table.schema()

    def blocks(self):
        return True

    def __component_eq__(
        self,
        other: Distinct,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return self.table.equals(other.table, cache=cache)


@public
class ExistsSubquery(Node):
    foreign_table = rlz.table
    predicates = rlz.list_of(rlz.boolean)

    def output_type(self):
        return ir.ExistsExpr

    def __component_eq__(
        self,
        other: ExistsSubquery,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return util.seq_eq(
            self.predicates,
            other.predicates,
            cache=cache,
        ) and self.foreign_table.equals(other.foreign_table, cache=cache)


@public
class NotExistsSubquery(Node):
    foreign_table = rlz.table
    predicates = rlz.list_of(rlz.boolean)

    def output_type(self):
        return ir.ExistsExpr

    def __component_eq__(
        self,
        other: NotExistsSubquery,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return util.seq_eq(
            self.predicates,
            other.predicates,
            cache=cache,
        ) and self.foreign_table.equals(other.foreign_table, cache=cache)


@public
class FillNa(TableNode, sch.HasSchema):
    """Fill null values in the table."""

    table = rlz.table
    replacements = rlz.one_of(
        (
            rlz.numeric,
            rlz.string,
            rlz.instance_of(collections.abc.Mapping),
        )
    )

    def __init__(self, table, replacements, **kwargs):
        super().__init__(
            table=table,
            replacements=(
                replacements
                if not isinstance(replacements, collections.abc.Mapping)
                else util.frozendict(replacements)
            ),
            **kwargs,
        )

    @property
    def schema(self):
        return self.table.schema()

    def __component_eq__(
        self,
        other: FillNa,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        self_repl = self.replacements
        other_repl = other.replacements
        if isinstance(self_repl, util.frozendict):
            return self_repl == other_repl and self.table.equals(
                other.table,
                cache=cache,
            )
        return util.seq_eq(
            self_repl,
            other_repl,
            cache=cache,
        ) and self.table.equals(other.table, cache=cache)


@public
class DropNa(TableNode, sch.HasSchema):
    """Drop null values in the table."""

    table = rlz.table
    how = rlz.isin({'any', 'all'})
    subset = rlz.optional(rlz.list_of(rlz.column_from("table")), default=())

    @property
    def schema(self):
        return self.table.schema()

    def __component_eq__(
        self,
        other: DropNa,
        cache: MutableMapping[Hashable, bool],
    ) -> bool:
        return (
            self.how == other.how
            and util.seq_eq(self.replacements, other.replacements, cache=cache)
            and self.table.equals(other.table, cache=cache)
        )


def _dedup_join_columns(
    expr: ir.TableExpr,
    *,
    left: ir.TableExpr,
    right: ir.TableExpr,
    suffixes: tuple[str, str],
):
    right_columns = frozenset(right.columns)
    overlap = frozenset(
        column for column in left.columns if column in right_columns
    )

    if not overlap:
        return expr

    left_suffix, right_suffix = suffixes

    left_projections = [
        left[column].name(f"{column}{left_suffix}")
        if column in overlap
        else left[column]
        for column in left.columns
    ]

    right_projections = [
        right[column].name(f"{column}{right_suffix}")
        if column in overlap
        else right[column]
        for column in right.columns
    ]
    return expr.projection(left_projections + right_projections)
