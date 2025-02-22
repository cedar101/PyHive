"""Integration between SQLAlchemy and Hive.

Some code based on
https://github.com/zzzeek/sqlalchemy/blob/rel_0_5/lib/sqlalchemy/databases/sqlite.py
which is released under the MIT license.
"""

from __future__ import absolute_import, unicode_literals, annotations

import datetime
import decimal

import re
from typing import Any

from sqlalchemy import exc
from sqlalchemy.sql import text

try:
    from sqlalchemy import processors
except ImportError:
    # Required for SQLAlchemy>=2.0
    from sqlalchemy.engine import processors
from sqlalchemy import types, util

# TODO shouldn't use mysql type
try:
    from sqlalchemy.databases import mysql

    mysql_tinyinteger = mysql.MSTinyInteger
except ImportError:
    # Required for SQLAlchemy>2.0
    from sqlalchemy.dialects import mysql

    mysql_tinyinteger = mysql.base.MSTinyInteger
from sqlalchemy.engine import default
from sqlalchemy.sql import compiler
from sqlalchemy.sql.compiler import SQLCompiler, DDLCompiler
from sqlalchemy.sql._typing import _TypeEngineArgument

from pyhive import hive
from pyhive.common import UniversalSet

from dateutil.parser import parse
from decimal import Decimal


class HiveStringTypeBase(types.TypeDecorator):
    """Translates strings returned by Thrift into something else"""

    impl = types.String

    def process_bind_param(self, value, dialect):
        raise NotImplementedError("Writing to Hive not supported")


class HiveDate(HiveStringTypeBase):
    """Translates date strings to date objects"""

    impl = types.DATE

    def process_result_value(self, value, dialect):
        return processors.str_to_date(value)

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, datetime.datetime):
                return value.date()
            elif isinstance(value, datetime.date):
                return value
            elif value is not None:
                return parse(value).date()
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


class HiveTimestamp(HiveStringTypeBase):
    """Translates timestamp strings to datetime objects"""

    impl = types.TIMESTAMP

    def process_result_value(self, value, dialect):
        return processors.str_to_datetime(value)

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, datetime.datetime):
                return value
            elif value is not None:
                return parse(value)
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


class HiveDecimal(HiveStringTypeBase):
    """Translates strings to decimals"""

    impl = types.DECIMAL

    def process_result_value(self, value, dialect):
        if value is not None:
            return decimal.Decimal(value)
        else:
            return None

    def result_processor(self, dialect, coltype):
        def process(value):
            if isinstance(value, Decimal):
                return value
            elif value is not None:
                return Decimal(value)
            else:
                return None

        return process

    def adapt(self, impltype, **kwargs):
        return self.impl


# from dialects/postgresql/array.py


class HiveArray(types.ARRAY):
    """
    Hive ARRAY type.

    The :class:`_sqlalchemy_hive.HiveArray` type is constructed in the same way
    as the core :class:`_types.ARRAY` type; a member type is required, and a
    number of dimensions is recommended if the type is to be used for more
    than one dimension::

        from pyhive.sqlalchemy_hive import HiveArray

        mytable = Table(
            "mytable",
            metadata,
            Column("data", HiveArray(Integer, dimensions=2)),
        )


    Indexed access is one-based by default, to match that of PostgreSQL;
    for zero-based indexed access, set
    :paramref:`_sqlalchemy_hive.HiveArray.zero_indexes`.

    .. seealso::

        :class:`_types.ARRAY` - base array type

    """

    def __init__(
        self,
        item_type: _TypeEngineArgument[Any],
        dimensions: int | None = None,
        zero_indexes: bool = False,
    ):
        """Construct an ARRAY.

        E.g.::

          Column("myarray", HiveArray(Integer))

        Arguments are:

        :param item_type: The data type of items of this array. Note that
          dimensionality is irrelevant here, so multi-dimensional arrays like
          ``INTEGER[][]``, are constructed as ``HiveArray(Integer)``, not as
          ``HiveArray(HiveArray(Integer))`` or such.

        :param dimensions: if non-None, the ARRAY will assume a fixed
         number of dimensions.  This will cause the DDL emitted for this
         ARRAY to include the exact number of bracket clauses ``[]``,
         and will also optimize the performance of the type overall.
         Note that arrays are always implicitly "non-dimensioned",
         meaning they can store any number of dimensions no matter how
         they were declared.

        :param zero_indexes=False: when True, index values will be converted
         between Python zero-based and Hive one-based indexes, e.g.
         a value of one will be added to all index values before passing
         to the database.

        """
        if isinstance(item_type, HiveArray):
            raise ValueError(
                "Do not nest ARRAY types; ARRAY(basetype) "
                "handles multi-dimensional arrays of basetype"
            )
        if isinstance(item_type, type):
            item_type = item_type()
        if isinstance(item_type, str):
            item_type = _type_map[item_type]

        self.item_type = item_type
        self.dimensions = dimensions
        self.zero_indexes = zero_indexes

    @property
    def python_type(self):
        return list

    def compare_values(self, x, y):
        return x == y

    @util.memoized_property
    def _against_native_enum(self):
        return isinstance(self.item_type, types.Enum) and self.item_type.native_enum

    def literal_processor(self, dialect):
        item_proc = self.item_type.dialect_impl(dialect).literal_processor(dialect)
        if item_proc is None:
            return None

        def to_str(elements):
            return f"[{', '.join(elements)}]"

        def process(value):
            inner = self._apply_item_processor(
                value, item_proc, self.dimensions, to_str
            )
            return inner

        return process

    def bind_processor(self, dialect):
        item_proc = self.item_type.dialect_impl(dialect).bind_processor(dialect)

        def process(value):
            if value is None:
                return value
            else:
                return self._apply_item_processor(
                    value, item_proc, self.dimensions, list
                )

        return process

    def result_processor(self, dialect, coltype):
        item_proc = self.item_type.dialect_impl(dialect).result_processor(
            dialect, coltype
        )

        def process(value):
            if value is None:
                return value
            else:
                return self._apply_item_processor(
                    value,
                    item_proc,
                    self.dimensions,
                    tuple if self.as_tuple else list,
                )

        return process


class HiveIdentifierPreparer(compiler.IdentifierPreparer):
    # Just quote everything to make things simpler / easier to upgrade
    reserved_words = UniversalSet()

    def __init__(self, dialect):
        super(HiveIdentifierPreparer, self).__init__(
            dialect,
            initial_quote="`",
        )


_type_map = {
    "boolean": types.Boolean,
    "tinyint": mysql_tinyinteger,
    "smallint": types.SmallInteger,
    "int": types.Integer,
    "bigint": types.BigInteger,
    "float": types.Float,
    "double": types.Float,
    "string": types.String,
    "varchar": types.String,
    "char": types.String,
    "date": HiveDate,
    "timestamp": HiveTimestamp,
    "binary": types.String,
    "array": HiveArray,
    "map": types.String,
    "struct": types.String,
    "uniontype": types.String,
    "decimal": HiveDecimal,
}


class HiveCompiler(SQLCompiler):
    insert_regex = re.compile(r"(INSERT INTO) ([^\s]+) \([^\)]*\)")
    insert_partition_regex = re.compile(
        r"(INSERT INTO) ([^\s]+) (PARTITION \([^\)]+\)) \([^\)]*\)"
    )

    def visit_concat_op_binary(self, binary, operator, **kw):
        return "concat(%s, %s)" % (
            self.process(binary.left),
            self.process(binary.right),
        )

    def visit_insert(self, *args, **kwargs):
        result = super(HiveCompiler, self).visit_insert(*args, **kwargs)
        # Massage the result into Hive's format
        #   INSERT INTO `pyhive_test_database`.`test_table` (`a`) SELECT ...
        #   =>
        #   INSERT INTO TABLE `pyhive_test_database`.`test_table` SELECT ...
        if self.__class__.insert_regex.search(result):
            return self.__class__.insert_regex.sub(r"\1 TABLE \2", result)

        assert self.__class__.insert_partition_regex.search(result), (
            f"Unexpected visit_insert result: {result}"
        )
        return self.__class__.insert_partition_regex.sub(r"\1 TABLE \2 \3", result)

    def visit_column(self, *args, **kwargs):
        result = super(HiveCompiler, self).visit_column(*args, **kwargs)
        dot_count = result.count(".")
        assert dot_count in (0, 1, 2), "Unexpected visit_column result {}".format(
            result
        )
        if dot_count == 2:
            # we have something of the form schema.table.column
            # hive doesn't like the schema in front, so chop it out
            result = result[result.index(".") + 1 :]
        return result

    def visit_char_length_func(self, fn, **kw):
        return "length{}".format(self.function_argspec(fn, **kw))

    def get_from_hint_text(self, table, text):
        return text

    def get_crud_hint_text(self, table, text):
        return text

    def visit_array(self, element, **kw):
        return f"[{self.visit_clauselist(element, **kw)}]"

    def visit_regexp_match_op_binary(self, binary, operator, **kw):
        return self._generate_generic_binary(binary, " REGEXP ", **kw)

    def visit_not_regexp_match_op_binary(self, binary, operator, **kw):
        return self._generate_generic_binary(binary, " NOT REGEXP ", **kw)


class HiveDDLCompiler(DDLCompiler):
    """
    Sources
    * https://github.com/cloudera/impyla/blob/master/impala/sqlalchemy.py
    * https://github.com/sqlalchemy/lib/sqlalchemy/sql/compiler.py
    """

    def visit_primary_key_constraint(self, constraint, **kw):
        return f"{super().visit_primary_key_constraint(constraint)} DISABLE NOVALIDATE RELY"

    def visit_foreign_key_constraint(self, constraint, **kw):
        return f"{super().visit_foreign_key_constraint(constraint)} DISABLE NOVALIDATE"

    def post_create_table(self, table):
        """Build table-level CREATE options."""

        def table_opts(table):
            if table.comment:
                yield f"COMMENT '{table.comment}'"

            if "hive_partitioned_by" in table.kwargs:
                yield f"PARTITIONED BY {table.kwargs['hive_partitioned_by']}"

            if "hive_clustered_by" in table.kwargs:
                yield f"CLUSTERED BY {table.kwargs['hive_clustered_by']}"

            if "hive_stored_as" in table.kwargs:
                yield f"STORED AS {table.kwargs['hive_stored_as']}"

            if "hive_table_properties" in table.kwargs:
                table_properties = [
                    f"'{property_}' = '{value}'"
                    for property_, value in sorted(
                        table.kwargs["hive_table_properties"].items()
                    )
                ]
                yield f"TBLPROPERTIES ({', '.join(table_properties)})"

        return f"\n{'\n'.join(table_opts(table))}"

    def visit_create_column(self, create, first_pk=False, **kw):
        text = super().visit_create_column(create, first_pk, **kw)
        comment = create.element.comment
        if comment:
            text += f" COMMENT '{comment}'"
        return text


class HiveTypeCompiler(compiler.GenericTypeCompiler):
    def visit_INTEGER(self, type_):
        return "INT"

    def visit_NUMERIC(self, type_):
        return "DECIMAL"

    def visit_CHAR(self, type_):
        return "STRING"

    def visit_VARCHAR(self, type_):
        return "STRING"

    def visit_NCHAR(self, type_):
        return "STRING"

    def visit_TEXT(self, type_):
        return "STRING"

    def visit_CLOB(self, type_):
        return "STRING"

    def visit_BLOB(self, type_):
        return "BINARY"

    def visit_TIME(self, type_):
        return "TIMESTAMP"

    def visit_DATE(self, type_):
        return "DATE"

    def visit_DATETIME(self, type_):
        return "TIMESTAMP"

    def visit_ARRAY(self, type_, **kw):
        inner = self.process(type_.item_type, **kw)
        return f"ARRAY<{inner}>"


class HiveExecutionContext(default.DefaultExecutionContext):
    """This is pretty much the same as SQLiteExecutionContext to work around the same issue.

    http://docs.sqlalchemy.org/en/latest/dialects/sqlite.html#dotted-column-names

    engine = create_engine('hive://...', execution_options={'hive_raw_colnames': True})
    """

    @util.memoized_property
    def _preserve_raw_colnames(self):
        # Ideally, this would also gate on hive.resultset.use.unique.column.names
        return self.execution_options.get("hive_raw_colnames", False)

    def _translate_colname(self, colname):
        # Adjust for dotted column names.
        # When hive.resultset.use.unique.column.names is true (the default), Hive returns column
        # names as "tablename.colname" in cursor.description.
        if not self._preserve_raw_colnames and "." in colname:
            return colname.split(".")[-1], colname
        else:
            return colname, None


class HiveDialect(default.DefaultDialect):
    name = "hive"
    driver = "thrift"
    execution_ctx_cls = HiveExecutionContext
    preparer = HiveIdentifierPreparer
    statement_compiler = HiveCompiler
    type_compiler = HiveTypeCompiler
    ddl_compiler = HiveDDLCompiler
    supports_views = True
    supports_alter = True
    supports_pk_autoincrement = False
    supports_default_values = False
    supports_empty_insert = False
    supports_native_decimal = True
    supports_native_boolean = True
    supports_unicode_statements = True
    supports_unicode_binds = True
    returns_unicode_strings = True
    description_encoding = None
    supports_multivalues_insert = True
    supports_sane_rowcount = False
    supports_statement_cache = False

    @classmethod
    def dbapi(cls):
        return hive

    @classmethod
    def import_dbapi(cls):
        return hive

    def create_connect_args(self, url):
        kwargs = {
            "host": url.host,
            "port": url.port or 10000,
            "username": url.username,
            "password": url.password,
            "database": url.database or "default",
        }
        kwargs.update(url.query)
        return [], kwargs

    def get_schema_names(self, connection, **kw):
        # Equivalent to SHOW DATABASES
        return [row[0] for row in connection.execute(text("SHOW SCHEMAS"))]

    def get_view_names(self, connection, schema=None, **kw):
        # Hive does not provide functionality to query tableType
        # This allows reflection to not crash at the cost of being inaccurate
        return self.get_table_names(connection, schema, **kw)

    def _get_table_columns(self, connection, table_name, schema):
        full_table = table_name
        if schema:
            full_table = schema + "." + table_name
        # TODO using TGetColumnsReq hangs after sending TFetchResultsReq.
        # Using DESCRIBE works but is uglier.
        try:
            # This needs the table name to be unescaped (no backticks).
            rows = connection.execute(text("DESCRIBE {}".format(full_table))).fetchall()
        except exc.OperationalError as e:
            # Does the table exist?
            regex_fmt = r"TExecuteStatementResp.*SemanticException.*Table not found {}"
            regex = regex_fmt.format(re.escape(full_table))
            if re.search(regex, e.args[0]):
                raise exc.NoSuchTableError(full_table)
            else:
                raise
        else:
            # Hive is stupid: this is what I get from DESCRIBE some_schema.does_not_exist
            regex = r"Table .* does not exist"
            if len(rows) == 1 and re.match(regex, rows[0].col_name):
                raise exc.NoSuchTableError(full_table)
            return rows

    def has_table(self, connection, table_name, schema=None, **kw):
        try:
            self._get_table_columns(connection, table_name, schema)
            return True
        except exc.NoSuchTableError:
            return False

    def get_columns(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        # Strip whitespace
        rows = [[col.strip() if col else None for col in row] for row in rows]
        # Filter out empty rows and comment
        rows = [row for row in rows if row[0] and row[0] != "# col_name"]
        result = []
        for col_name, col_type, _comment in rows:
            if col_name == "# Partition Information":
                break

            # Take out the more detailed type information
            # e.g. 'map<int,int>' -> 'map'
            #      'decimal(10,1)' -> decimal
            col_type_name = re.search(r"^\w+", col_type).group(0)
            try:
                mapped_type = _type_map[col_type_name]
            except KeyError:
                util.warn(
                    "Did not recognize type '%s' of column '%s'" % (col_type, col_name)
                )
                mapped_type = types.NullType

            array_match = re.search(r"^array<(\w+)>", col_type, re.I)
            if array_match is not None:
                mapped_type.item_type = _type_map[array_match.group(1)]

            result.append(
                {
                    "name": col_name,
                    "type": mapped_type,
                    "nullable": True,
                    "default": None,
                    "comment": _comment,
                }
            )
        return result

    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # Hive has no support for foreign keys.
        return []

    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        # Hive has no support for primary keys.
        return []

    def get_indexes(self, connection, table_name, schema=None, **kw):
        rows = self._get_table_columns(connection, table_name, schema)
        # Strip whitespace
        rows = [[col.strip() if col else None for col in row] for row in rows]
        # Filter out empty rows and comment
        rows = [row for row in rows if row[0] and row[0] != "# col_name"]
        for i, (col_name, _col_type, _comment) in enumerate(rows):
            if col_name == "# Partition Information":
                break
        # Handle partition columns
        col_names = []
        for col_name, _col_type, _comment in rows[i + 1 :]:
            col_names.append(col_name)
        if col_names:
            return [{"name": "partition", "column_names": col_names, "unique": False}]
        else:
            return []

    def get_table_names(self, connection, schema=None, **kw):
        query = "SHOW TABLES"
        if schema:
            query += " IN " + self.identifier_preparer.quote_identifier(schema)
        return [row[0] for row in connection.execute(text(query))]

    def do_rollback(self, dbapi_connection):
        # No transactions for Hive
        pass

    def _check_unicode_returns(self, connection, additional_tests=None):
        # We decode everything as UTF-8
        return True

    def _check_unicode_description(self, connection):
        # We decode everything as UTF-8
        return True


class HiveHTTPDialect(HiveDialect):
    name = "hive"
    scheme = "http"
    driver = "rest"

    def create_connect_args(self, url):
        kwargs = {
            "host": url.host,
            "port": url.port or 10000,
            "scheme": self.scheme,
            "username": url.username or None,
            "password": url.password or None,
        }
        if url.query:
            kwargs.update(url.query)
            return [], kwargs
        return ([], kwargs)


class HiveHTTPSDialect(HiveHTTPDialect):
    name = "hive"
    scheme = "https"
