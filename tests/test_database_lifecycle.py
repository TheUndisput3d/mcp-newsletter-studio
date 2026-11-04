"""Transactions must commit/rollback and close their SQLite handles."""
import sqlite3

import pytest
from newsletter_mcp.state import StateStore
from newsletter_mcp.key_pool import KeyPool

@pytest.mark.parametrize('factory,method', [(StateStore,'connect'),(KeyPool,'db')])
def test_connections_close_on_success_and_exception(settings,factory,method):
    store=factory(settings.workspace) if factory is StateStore else factory(settings)
    connection=getattr(store,method)
    with connection() as db:
        db.execute('CREATE TABLE lifecycle_probe (value INTEGER)')
        db.execute('INSERT INTO lifecycle_probe VALUES(1)')
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        db.execute('SELECT 1')
    with pytest.raises(RuntimeError,match='abort transaction'):
        with connection() as failed:
            failed.execute('INSERT INTO lifecycle_probe VALUES(2)')
            raise RuntimeError('abort transaction')
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        failed.execute('SELECT 1')
    with connection() as verified:
        assert [row[0] for row in verified.execute('SELECT value FROM lifecycle_probe')]==[1]
