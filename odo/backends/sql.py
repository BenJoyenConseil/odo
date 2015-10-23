from __future__ import absolute_import, division, print_function

from operator import attrgetter
import os
import re
import subprocess

from itertools import chain
from collections import Iterator
from datetime import datetime, date
from distutils.spawn import find_executable

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import event
from sqlalchemy.schema import CreateSchema

from multipledispatch import MDNotImplementedError

import datashape
from datashape import DataShape, Record, Option, var, dshape, Map
from datashape.predicates import isdimension, isrecord, isscalar
from datashape import discover, datetime_, date_, float64, int64, int_, string
from datashape import float32
from datashape.dispatch import dispatch

from toolz import (partition_all, keyfilter, memoize, valfilter, identity,
                   concat, curry, merge)
from toolz.curried import pluck, map

from ..compatibility import unicode
from ..utils import keywords, ignoring, iter_except, filter_kwargs
from ..convert import convert, ooc_types
from ..append import append
from ..resource import resource
from ..chunks import Chunks
from .csv import CSV

base = (int, float, datetime, date, bool, str)


# http://docs.sqlalchemy.org/en/latest/core/types.html

types = {
    'int64': sa.BigInteger,
    'int32': sa.Integer,
    'int': sa.Integer,
    'int16': sa.SmallInteger,
    'float32': sa.REAL,
    'float64': sa.FLOAT,
    'float': sa.FLOAT,
    'real': sa.FLOAT,
    'string': sa.Text,
    'date': sa.Date,
    'time': sa.Time,
    'datetime': sa.DateTime,
    'bool': sa.Boolean,
    "timedelta[unit='D']": sa.Interval(second_precision=0, day_precision=9),
    "timedelta[unit='h']": sa.Interval(second_precision=0, day_precision=0),
    "timedelta[unit='m']": sa.Interval(second_precision=0, day_precision=0),
    "timedelta[unit='s']": sa.Interval(second_precision=0, day_precision=0),
    "timedelta[unit='ms']": sa.Interval(second_precision=3, day_precision=0),
    "timedelta[unit='us']": sa.Interval(second_precision=6, day_precision=0),
    "timedelta[unit='ns']": sa.Interval(second_precision=9, day_precision=0),
    # ??: sa.types.LargeBinary,
}

revtypes = dict(map(reversed, types.items()))

revtypes.update({
    sa.DATETIME: datetime_,
    sa.TIMESTAMP: datetime_,
    sa.FLOAT: float64,
    sa.DATE: date_,
    sa.BIGINT: int64,
    sa.INTEGER: int_,
    sa.BIGINT: int64,
    sa.types.NullType: string,
    sa.REAL: float32,
    sa.Float: float64,
    sa.Float(precision=24): float32,
    sa.Float(precision=53): float64,
})

# interval types are special cased in discover_typeengine so remove them from
# revtypes
revtypes = valfilter(lambda x: not isinstance(x, sa.Interval), revtypes)


units_of_power = {
    0: 's',
    3: 'ms',
    6: 'us',
    9: 'ns'
}

# these aren't loaded by sqlalchemy by default
sa.dialects.registry.load('oracle')
sa.dialects.registry.load('postgresql')


def getbind(t, bind):
    if bind is None:
        return t.bind

    if isinstance(bind, sa.engine.base.Engine):
        return bind
    return sa.create_engine(bind)


def batch(sel, chunksize=10000, bind=None):
    """Execute `sel`, streaming row at a time and fetching from the database in
    batches of size `chunksize`.

    Parameters
    ----------
    sel : sa.sql.Selectable
        Selectable to execute
    chunksize : int, optional, default 10000
        Number of rows to fetch from the database
    """
    def rowterator(sel, chunksize=chunksize):
        with getbind(sel, bind).connect() as conn:
            result = conn.execute(sel)
            yield result.keys()

            for rows in iter_except(curry(result.fetchmany, size=chunksize),
                                    sa.exc.ResourceClosedError):
                if rows:
                    yield rows
                else:
                    return
    terator = rowterator(sel)
    return next(terator), concat(terator)


@discover.register(sa.dialects.postgresql.base.INTERVAL)
def discover_postgresql_interval(t):
    return discover(sa.Interval(day_precision=0, second_precision=t.precision))


@discover.register(sa.dialects.oracle.base.INTERVAL)
def discover_oracle_interval(t):
    return discover(t.adapt(sa.Interval))


@discover.register(sa.sql.type_api.TypeEngine)
def discover_typeengine(typ):
    if isinstance(typ, sa.Interval):
        if typ.second_precision is None and typ.day_precision is None:
            return datashape.TimeDelta(unit='us')
        elif typ.second_precision == 0 and typ.day_precision == 0:
            return datashape.TimeDelta(unit='s')

        if typ.second_precision in units_of_power and not typ.day_precision:
            units = units_of_power[typ.second_precision]
        elif typ.day_precision > 0:
            units = 'D'
        else:
            raise ValueError('Cannot infer INTERVAL type with parameters'
                             'second_precision=%d, day_precision=%d' %
                             (typ.second_precision, typ.day_precision))
        return datashape.TimeDelta(unit=units)
    if typ in revtypes:
        return dshape(revtypes[typ])[0]
    if type(typ) in revtypes:
        return revtypes[type(typ)]
    if isinstance(typ, (sa.NUMERIC, sa.DECIMAL)):
        return datashape.Decimal(precision=typ.precision, scale=typ.scale)
    if isinstance(typ, (sa.String, sa.Unicode)):
        return datashape.String(typ.length, typ.collation)
    else:
        for k, v in revtypes.items():
            if isinstance(k, type) and (isinstance(typ, k) or
                                        hasattr(typ, 'impl') and
                                        isinstance(typ.impl, k)):
                return v
            if k == typ:
                return v
    raise NotImplementedError("No SQL-datashape match for type %s" % typ)


@discover.register(sa.ForeignKey, sa.sql.FromClause)
def discover_foreign_key_relationship(fk, parent, parent_measure=None):
    if fk.column.table is not parent:
        parent_measure = discover(fk.column.table).measure
    return {fk.parent.name: Map(discover(fk.parent.type), parent_measure)}


@discover.register(sa.Column)
def discover_sqlalchemy_column(c):
    meta = Option if c.nullable else identity
    return Record([(c.name, meta(discover(c.type)))])


@discover.register(sa.sql.FromClause)
def discover_sqlalchemy_selectable(t):
    ordering = dict((c, i) for i, c in enumerate(c for c in t.columns.keys()))
    records = list(sum([discover(c).parameters[0] for c in t.columns], ()))
    fkeys = [discover(fkey, t, parent_measure=Record(records))
             for fkey in t.foreign_keys]
    for name, column in merge(*fkeys).items():
        records[ordering[name]] = (name, column)
    return var * Record(records)


@memoize
def metadata_of_engine(engine, schema=None):
    return sa.MetaData(engine, schema=schema)


def create_engine(uri, *args, **kwargs):
    if ':memory:' in uri:
        return sa.create_engine(uri, *args, **kwargs)
    else:
        return memoized_create_engine(uri, *args, **kwargs)


memoized_create_engine = memoize(sa.create_engine)


@dispatch(sa.engine.base.Engine, str)
def discover(engine, tablename):
    metadata = metadata_of_engine(engine)
    if tablename not in metadata.tables:
        try:
            metadata.reflect(engine,
                             views=metadata.bind.dialect.supports_views)
        except NotImplementedError:
            metadata.reflect(engine)
    table = metadata.tables[tablename]
    return discover(table)


@dispatch(sa.engine.base.Engine)
def discover(engine):
    metadata = metadata_of_engine(engine)
    return discover(metadata)


@dispatch(sa.MetaData)
def discover(metadata):
    try:
        metadata.reflect(views=metadata.bind.dialect.supports_views)
    except NotImplementedError:
        metadata.reflect()
    pairs = []
    for table in sorted(metadata.tables.values(), key=attrgetter('name')):
        name = table.name
        try:
            pairs.append([name, discover(table)])
        except sa.exc.CompileError as e:
            print("Can not discover type of table %s.\n" % name +
                  "SQLAlchemy provided this error message:\n\t%s" % e.message +
                  "\nSkipping.")
        except NotImplementedError as e:
            print("Blaze does not understand a SQLAlchemy type.\n"
                  "Blaze provided the following error:\n\t%s" % "\n\t".join(e.args) +
                  "\nSkipping.")
    return DataShape(Record(pairs))


@discover.register(sa.engine.RowProxy)
def discover_row_proxy(rp):
    return Record(list(zip(rp.keys(), map(discover, rp.values()))))


def validate_foreign_keys(ds, foreign_keys):
    # passed foreign_keys and column in dshape, but not a ForeignKey type
    for field in foreign_keys:
        if field not in ds.measure.names:
            raise TypeError('Requested foreign key field %r is not a field in '
                            'datashape %s' % (field, ds))
    for field, typ in ds.measure.fields:
        if field in foreign_keys and not isinstance(getattr(typ, 'ty', typ),
                                                    Map):
            raise TypeError('Foreign key %s passed in but not a Map '
                            'datashape, got %s' % (field, typ))

        if isinstance(typ, Map) and field not in foreign_keys:
            raise TypeError('Map type %s found on column %s, but %r '
                            "wasn't found in %s" %
                            (typ, field, field, foreign_keys))


def dshape_to_table(name, ds, metadata=None, foreign_keys=None,
                    primary_key=None):
    """
    Create a SQLAlchemy table from a datashape and a name

    >>> dshape_to_table('bank', '{name: string, amount: int}') # doctest: +NORMALIZE_WHITESPACE
    Table('bank', MetaData(bind=None),
          Column('name', Text(), table=<bank>, nullable=False),
          Column('amount', Integer(), table=<bank>, nullable=False),
          schema=None)
    """

    if isinstance(ds, str):
        ds = dshape(ds)
    if not isrecord(ds.measure):
        raise TypeError('dshape measure must be a record type e.g., '
                        '"{a: int64, b: int64}". Input measure is %r' %
                        ds.measure)
    if metadata is None:
        metadata = sa.MetaData()
    if foreign_keys is None:
        foreign_keys = {}

    validate_foreign_keys(ds, foreign_keys)

    cols = dshape_to_alchemy(ds, primary_key=primary_key or frozenset())
    cols.extend(sa.ForeignKeyConstraint([column_name], [referent])
                for column_name, referent in foreign_keys.items())
    t = sa.Table(name, metadata, *cols, schema=metadata.schema)
    return attach_schema(t, t.schema)


@dispatch(object, str)
def create_from_datashape(o, ds, **kwargs):
    return create_from_datashape(o, dshape(ds), **kwargs)


@dispatch(sa.engine.base.Engine, DataShape)
def create_from_datashape(engine, ds, schema=None, foreign_keys=None,
                          primary_key=None, **kwargs):
    assert isrecord(ds), 'datashape must be Record type, got %s' % ds
    metadata = metadata_of_engine(engine, schema=schema)
    for name, sub_ds in ds[0].dict.items():
        t = dshape_to_table(name, sub_ds, metadata=metadata,
                            foreign_keys=foreign_keys,
                            primary_key=primary_key)
        t.create()
    return engine


def dshape_to_alchemy(dshape, primary_key=frozenset()):
    """

    >>> dshape_to_alchemy('int')
    <class 'sqlalchemy.sql.sqltypes.Integer'>

    >>> dshape_to_alchemy('string')
    <class 'sqlalchemy.sql.sqltypes.Text'>

    >>> dshape_to_alchemy('{name: string, amount: int}')
    [Column('name', Text(), table=None, nullable=False), Column('amount', Integer(), table=None, nullable=False)]

    >>> dshape_to_alchemy('{name: ?string, amount: ?int}')
    [Column('name', Text(), table=None), Column('amount', Integer(), table=None)]
    """
    if isinstance(dshape, str):
        dshape = datashape.dshape(dshape)
    if isinstance(dshape, Map):
        return dshape_to_alchemy(dshape.key.measure, primary_key=primary_key)
    if isinstance(dshape, Option):
        return dshape_to_alchemy(dshape.ty, primary_key=primary_key)
    if str(dshape) in types:
        return types[str(dshape)]
    if isinstance(dshape, datashape.Record):
        return [sa.Column(name,
                          dshape_to_alchemy(getattr(typ, 'ty', typ),
                                            primary_key=primary_key),
                          primary_key=name in primary_key,
                          nullable=isinstance(typ[0], Option))
                for name, typ in dshape.parameters[0]]
    if isinstance(dshape, datashape.DataShape):
        if isdimension(dshape[0]):
            return dshape_to_alchemy(dshape[1], primary_key=primary_key)
        else:
            return dshape_to_alchemy(dshape[0], primary_key=primary_key)
    if isinstance(dshape, datashape.String):
        fixlen = dshape[0].fixlen
        if fixlen is None:
            return sa.TEXT
        string_types = dict(U=sa.Unicode, A=sa.String)
        assert dshape.encoding is not None
        return string_types[dshape.encoding[0]](length=fixlen)
    if isinstance(dshape, datashape.DateTime):
        return sa.DATETIME(timezone=dshape.tz is not None)
    if isinstance(dshape, datashape.Decimal):
        return sa.NUMERIC(dshape.precision, dshape.scale)
    raise NotImplementedError("No SQLAlchemy dtype match for datashape: %s"
                              % dshape)


@convert.register(Iterator, sa.Table, cost=300.0)
def sql_to_iterator(t, bind=None, **kwargs):
    _, rows = batch(sa.select([t]), bind=bind)
    return map(tuple, rows)


@convert.register(Iterator, sa.sql.Select, cost=300.0)
def select_to_iterator(sel, dshape=None, bind=None, **kwargs):
    func = pluck(0) if dshape and isscalar(dshape.measure) else map(tuple)
    _, rows = batch(sel, bind=bind)
    return func(rows)


@convert.register(base, sa.sql.Select, cost=300.0)
def select_to_base(sel, dshape=None, bind=None, **kwargs):
    assert not dshape or isscalar(dshape), \
        'dshape should be None or scalar, got %s' % dshape
    with getbind(sel, bind).connect() as conn:
        return conn.execute(sel).scalar()


@append.register(sa.Table, Iterator)
def append_iterator_to_table(t, rows, dshape=None, bind=None, **kwargs):
    assert not isinstance(t, type)
    engine = getbind(t, bind)
    if not t.exists(bind=engine):
        t.create(bind=engine)
    rows = iter(rows)

    # We see if the sequence is of tuples or dicts
    # If tuples then we coerce them to dicts
    try:
        row = next(rows)
    except StopIteration:
        return
    rows = chain([row], rows)
    if isinstance(row, (tuple, list)):
        dshape = dshape and datashape.dshape(dshape)
        if dshape and isinstance(dshape.measure, datashape.Record):
            names = dshape.measure.names
            if set(names) != set(discover(t).measure.names):
                raise ValueError("Column names of incoming data don't match "
                                 "column names of existing SQL table\n"
                                 "Names in SQL table: %s\n"
                                 "Names from incoming data: %s\n" %
                                 (discover(t).measure.names, names))
        else:
            names = discover(t).measure.names
        rows = (dict(zip(names, row)) for row in rows)

    with engine.connect() as conn:
        for chunk in partition_all(1000, rows):  # TODO: 1000 is hardcoded
            conn.execute(t.insert(), chunk)

    return t


@append.register(sa.Table, Chunks)
def append_anything_to_sql_Table(t, c, **kwargs):
    for item in c:
        append(t, item, **kwargs)
    return t


@append.register(sa.Table, object)
def append_anything_to_sql_Table(t, o, **kwargs):
    return append(t, convert(Iterator, o, **kwargs), **kwargs)


@append.register(sa.Table, sa.Table)
def append_table_to_sql_Table(t, o, **kwargs):
    s = sa.select([o])
    return append(t, s, **kwargs)


@append.register(sa.Table, sa.sql.Select)
def append_select_statement_to_sql_Table(t, o, bind=None, **kwargs):
    t_bind = getbind(t, bind)
    o_bind = getbind(o, bind)
    if t_bind != o_bind:
        return append(
            t,
            convert(Iterator, o, bind=bind, **kwargs),
            bind=bind,
            **kwargs
        )
    bind = t_bind

    assert bind.has_table(t.name, t.schema), \
        'tables must come from the same database'

    query = t.insert().from_select(o.columns.keys(), o)

    with bind.connect() as conn:
        conn.execute(query)
    return t


def should_create_schema(ddl, target, bind, tables=None, state=None,
                         checkfirst=None, **kwargs):
    return ddl.element not in inspect(target.bind).get_schema_names()


def attach_schema(obj, schema):
    if schema is not None:
        ddl = CreateSchema(schema, quote=True)
        event.listen(obj,
                     'before_create',
                     ddl.execute_if(callable_=should_create_schema,
                                    dialect='postgresql'))
    return obj


def fullname(table, compiler):
    preparer = compiler.dialect.identifier_preparer
    fullname = preparer.quote_identifier(table.name)
    schema = table.schema
    if schema is not None:
        fullname = '%s.%s' % (preparer.quote_schema(schema), fullname)
    return fullname


@resource.register(r'(.*sql.*|oracle|redshift)(\+\w+)?://.+')
def resource_sql(uri, *args, **kwargs):
    engine = create_engine(uri, **filter_kwargs(sa.create_engine, kwargs))
    ds = kwargs.pop('dshape', None)
    schema = kwargs.pop('schema', None)
    foreign_keys = kwargs.pop('foreign_keys', None)
    primary_key = kwargs.pop('primary_key', None)

    # we were also given a table name
    if args and isinstance(args[0], (str, unicode)):
        table_name, args = args[0], args[1:]
        metadata = metadata_of_engine(engine, schema=schema)

        with ignoring(sa.exc.NoSuchTableError):
            return attach_schema(
                sa.Table(table_name, metadata, schema=schema,
                         autoload_with=engine),
                schema
            )
        if ds:
            t = dshape_to_table(table_name, ds, metadata=metadata,
                                foreign_keys=foreign_keys,
                                primary_key=primary_key)
            t.create()
            return t
        else:
            raise ValueError("Table does not exist and no dshape provided")

    # We were not given a table name
    if ds:
        create_from_datashape(engine, ds, schema=schema,
                              foreign_keys=foreign_keys)
    return engine


@resource.register('impala://.+')
def resource_impala(uri, *args, **kwargs):
    try:
        import impala.sqlalchemy
    except ImportError:
        raise ImportError("Please install or update `impyla` library")
    return resource_sql(uri, *args, **kwargs)


@resource.register('monetdb://.+')
def resource_monet(uri, *args, **kwargs):
    try:
        import monetdb
    except ImportError:
        raise ImportError("Please install the `sqlalchemy_monetdb` library")
    return resource_sql(uri, *args, **kwargs)


@resource.register('hive://.+')
def resource_hive(uri, *args, **kwargs):
    try:
        import pyhive
    except ImportError:
        raise ImportError("Please install the `PyHive` library.")

    pattern = 'hive://((?P<user>[a-zA-Z_]\w*)@)?(?P<host>[\w.]+)(:(?P<port>\d*))?(/(?P<database>\w*))?'
    d = re.search(pattern, uri.split('::')[0]).groupdict()

    defaults = {'port': '10000',
                'user': 'hdfs',
                'database': 'default'}
    for k, v in d.items():
        if not v:
            d[k] = defaults[k]

    if d['user']:
        d['user'] += '@'

    uri2 = 'hive://%(user)s%(host)s:%(port)s/%(database)s' % d
    if '::' in uri:
        uri2 += '::' + uri.split('::')[1]

    return resource_sql(uri2, *args, **kwargs)


ooc_types.add(sa.Table)


@dispatch(sa.Table)
def drop(table, bind=None):
    bind = getbind(table, bind)
    table.drop(bind=bind, checkfirst=True)
    if table.exists(bind=bind):
        raise ValueError('table %r dropped but still exists' % table.name)


@convert.register(pd.DataFrame, (sa.sql.Select, sa.sql.Selectable), cost=200.0)
def select_or_selectable_to_frame(el, bind=None, **kwargs):
    columns, rows = batch(el, bind=bind)
    row = next(rows, None)
    if row is None:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(list(chain([tuple(row)], map(tuple, rows))),
                        columns=columns)


class CopyToCSV(sa.sql.expression.Executable, sa.sql.ClauseElement):

    def __init__(self, element, path, delimiter=',', quotechar='"',
                 lineterminator=r'\n', escapechar='\\', header=True,
                 na_value='', bind=None):
        self.element = element
        self.path = path
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.lineterminator = lineterminator
        self._bind = bind = getbind(element, bind)

        # mysql cannot write headers
        self.header = header and bind.dialect.name != 'mysql'
        self.escapechar = escapechar
        self.na_value = na_value

    @property
    def bind(self):
        return self._bind


@compiles(CopyToCSV, 'postgresql')
def compile_copy_to_csv_postgres(element, compiler, **kwargs):
    selectable = element.element
    istable = isinstance(selectable, sa.Table)
    template = """COPY %s TO '{path}'
        WITH CSV {header}
        DELIMITER '{delimiter}'
        QUOTE '{quotechar}'
        NULL '{na_value}'
        ESCAPE '{escapechar}'
    """ % ('{query}' if istable else '({query})')
    processed = (fullname(selectable, compiler)
                 if istable else compiler.process(selectable))
    assert processed, ('got empty string from processing element of type %r' %
                       type(selectable).__name__)
    return template.format(query=processed,
                           path=element.path,
                           header='HEADER' if element.header else '',
                           delimiter=element.delimiter,
                           quotechar=element.quotechar,
                           na_value=element.na_value,
                           escapechar=element.escapechar)


@compiles(CopyToCSV, 'mysql')
def compile_copy_to_csv_mysql(element, compiler, **kwargs):
    selectable = element.element
    if isinstance(selectable, sa.Table):
        processed = 'SELECT * FROM %(table)s' % dict(table=selectable.name)
    else:
        processed = compiler.process(selectable)
    assert processed, ('got empty string from processing element of type %r' %
                       type(selectable).__name__)
    template = """{query} INTO OUTFILE '{path}'
    FIELDS TERMINATED BY '{delimiter}'
    OPTIONALLY ENCLOSED BY '{quotechar}'
    ESCAPED BY '{escapechar}'
    LINES TERMINATED BY '{lineterminator}'"""
    return template.format(query=processed,
                           path=element.path,
                           delimiter=element.delimiter,
                           lineterminator=element.lineterminator,
                           escapechar=element.escapechar.encode(
                               'unicode-escape').decode(),
                           quotechar=element.quotechar)


@compiles(CopyToCSV, 'sqlite')
def compile_copy_to_csv_sqlite(element, compiler, **kwargs):
    if not find_executable('sqlite3'):
        raise MDNotImplementedError("Could not find sqlite executable")

    selectable = element.element
    sql = (compiler.process(sa.select([selectable])
                            if isinstance(selectable, sa.Table)
                            else selectable) + ';')
    sql = re.sub(r'\s{2,}', ' ', re.sub(r'\s*\n\s*', ' ', sql)).encode()
    cmd = ['sqlite3', '-csv',
           '-%sheader' % ('no' if not element.header else ''),
           '-separator', element.delimiter,
           selectable.bind.url.database]
    with open(element.path, mode='at') as f:
        subprocess.Popen(cmd, stdout=f, stdin=subprocess.PIPE).communicate(sql)

    # This will be a no-op since we're doing the write during the compile
    return ''


@append.register(CSV, sa.sql.Selectable)
def append_table_to_csv(csv, selectable, dshape=None, bind=None, **kwargs):
    kwargs = keyfilter(keywords(CopyToCSV).__contains__,
                       merge(csv.dialect, kwargs))
    stmt = CopyToCSV(
        selectable,
        os.path.abspath(csv.path),
        bind=bind,
        **kwargs
    )
    with getbind(selectable, bind).begin() as conn:
        conn.execute(stmt)
    return csv


try:
    from .hdfs import HDFS
except ImportError:
    pass
else:
    @append.register(HDFS(CSV), sa.sql.Selectable)
    def append_selectable_to_hdfs_csv(*args, **kwargs):
        raise MDNotImplementedError()
