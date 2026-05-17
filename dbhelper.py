import logging
import sqlite3


LOG_SQL_TO_FILE = 0


log = logging.getLogger(__name__)

dbtype = None
db = None
param_mark = None

sqllog = None


def init(_dbtype, dbname):
    global dbtype, param_mark
    dbtype = _dbtype

    if dbtype == "pgsql":
        param_mark = "%s"
        init_pgsql(dbname)
    else:
        param_mark = "?"
        init_sqlite(dbname)


def init_sqlite(dbname):
    global db
    db = sqlite3.connect(dbname)
    db.row_factory = sqlite3.Row
    if LOG_SQL_TO_FILE:
        global sqllog
        sqllog = open(dbname + ".sql", "w")


def init_pgsql(dbname):
    import psycopg
    global db
    db = psycopg.connect(dbname, row_factory=psycopg.rows.namedtuple_row)
    execute_dml("SET session_replication_role = 'replica'")
    execute_dml("BEGIN")


def execute_dml(sql, values = (), returning=None):
    if returning:
        sql += " RETURNING " + returning
    if dbtype == "pgsql":
        sql = sql.replace("?", "%s")
    if LOG_SQL_TO_FILE:
        sqllog.write("%s %s\n" % (sql, values))
        return
    cursor = db.cursor()
    log.debug(sql + " " + str(values))
    cursor.execute(sql, values)
    if dbtype == "pgsql":
        if returning:
            return cursor.fetchone()[0]
    else:
        return cursor.lastrowid


def executescript(sql):
    cursor = db.cursor()
    log.debug(sql)
    if dbtype == "pgsql":
        cursor.execute(sql)
    else:
        cursor.executescript(sql)


def insert(table, fields=None, or_replace=False, returning=None, **kw):
    if fields is None:
        fields = kw

    safe_table = f'"{table}"' if table == "transaction" else table

    # 1. First-pass: Bootstrap the table dynamically if it doesn't exist
    if fields:
        try:
            # FIXED: Explicitly add an "_id" auto-incrementing primary key to every table
            columns_schema = ['"_id" INTEGER PRIMARY KEY AUTOINCREMENT']
            for key in fields.keys():
                if key != "_id":  # Avoid duplicates if _id is somehow already in fields
                    columns_schema.append(f'"{key}" TEXT')
            
            execute_dml(f'CREATE TABLE IF NOT EXISTS {safe_table} ({", ".join(columns_schema)});', [])
        except Exception as e:
            print(f"Initial schema build warning for {table}: {e}")

    # Build the standard SQL statement
    repl_clause = " OR REPLACE" if or_replace else ""
    field_names = [f'"{k}"' for k in fields.keys()]
    field_vals = list(fields.values())
    qmarks = [param_mark] * len(fields)
    sql = "INSERT%s INTO %s(%s) VALUES (%s)" % (repl_clause, safe_table, ", ".join(field_names), ", ".join(qmarks))

    # 2. Execute with our self-healing retry block for missing columns
    try:
        import sqlite3
        id = execute_dml(sql, field_vals, returning)
        return id
    except sqlite3.OperationalError as e:
        error_msg = str(e)
        if "has no column named" in error_msg:
            missing_col = error_msg.split("has no column named")[-1].strip()
            print(f"Patching schema: Adding missing column {missing_col} to table {table}...")
            
            try:
                execute_dml(f'ALTER TABLE {safe_table} ADD COLUMN "{missing_col}" TEXT;', [])
                id = execute_dml(sql, field_vals, returning)
                return id
            except Exception as retry_err:
                print(f"Failed to auto-heal schema for {table}: {retry_err}")
                raise retry_err
        else:
            raise e


def select(table, where=None, order=None):
    cursor = db.cursor()
    if where is None:
        where = ""
    else:
        where = " WHERE " + where
    if order is None:
        order = ""
    else:
        order = " ORDER BY " + order
    sql = "SELECT * FROM %s%s%s" % (table, where, order)
    if LOG_SQL_TO_FILE:
        sqllog.write("%s\n" % sql)
    cursor.execute(sql)
    log.debug(sql)
    return cursor.fetchall()


def commit():
    log.debug("COMMIT")
    db.commit()
