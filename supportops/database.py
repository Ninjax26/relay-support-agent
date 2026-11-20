"""Postgres support for the transactional Relay store."""
from contextlib import contextmanager


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, values=()):
        if statement == "BEGIN IMMEDIATE":
            # Match SQLite's single writer across concurrent function instances.
            return self.connection.execute("SELECT pg_advisory_xact_lock(76150213)")
        if statement == "PRAGMA journal_mode=WAL":
            return None
        sql = statement.replace("json_extract(runs.payload,'$.mode')", "(runs.payload::jsonb->>'mode')")
        sql = sql.replace("json_extract(runs.payload,'$.provider')", "(runs.payload::jsonb->>'provider')")
        sql = sql.replace("ORDER BY rowid LIMIT 1", "ORDER BY updated_at,ticket_id LIMIT 1")
        sql = sql.replace(" REAL", " DOUBLE PRECISION")
        return self.connection.execute(sql.replace("?", "%s"), values)

    def executescript(self, script):
        self.execute("BEGIN IMMEDIATE")
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)


class PostgresDatabase:
    def __init__(self, url):
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(url, min_size=0, max_size=8, timeout=10, open=True,
                                   kwargs={"connect_timeout": 7, "prepare_threshold": None})

    @contextmanager
    def connect(self):
        with self.pool.connection() as connection:
            yield PostgresConnection(connection)
