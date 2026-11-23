"""Install the initial laboratory schema on an empty MySQL database.

Also performs lightweight, idempotent upgrades for earlier snapshots:
newer columns, unique slots and the two fixed retest stations are added
without touching existing business rows.
"""
from sqlalchemy import inspect, select, text

from src import db, models
from src.models import RetestStation, STATION_COUNT


def add_missing_columns():
    inspector = inspect(db.engine)
    existing = {table: {column['name'] for column in inspector.get_columns(table)}
                for table in inspector.get_table_names()}
    statements = []
    if 'shrinkage_samples' in existing and 'claim_id' not in existing['shrinkage_samples']:
        statements.append('ALTER TABLE shrinkage_samples ADD COLUMN claim_id INTEGER NULL')
    with db.engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def ensure_stations():
    with db.Session() as session:
        present = set(session.scalars(select(RetestStation.id)).all())
        for station_id in range(1, STATION_COUNT + 1):
            if station_id not in present:
                session.add(RetestStation(id=station_id, name=f'复测工位 {station_id}'))
        session.commit()


if __name__ == '__main__':
    models.Base.metadata.create_all(db.engine)
    add_missing_columns()
    ensure_stations()
