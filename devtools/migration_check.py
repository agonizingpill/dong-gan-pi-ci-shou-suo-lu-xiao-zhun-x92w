"""迁移幂等性验证：全新库 + 模拟旧结构（缺 claim_id、缺新表）。"""
import sys

sys.path.insert(0, '/tmp/pylibs')
sys.path.insert(0, '/workspace/backend')

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import src.db as db_module
from src import models
from src.models import Base, RetestStation

fresh = create_engine('sqlite://')
db_module.engine = fresh
db_module.Session = sessionmaker(bind=fresh)

# 场景 A：全新库执行迁移
Base.metadata.create_all(fresh)
sys.path.insert(0, '/workspace/backend')
from migrations.initial import add_missing_columns, ensure_stations

add_missing_columns()
ensure_stations()
ensure_stations()  # 二次执行必须幂等
insp = inspect(fresh)
assert 'retest_stations' in insp.get_table_names()
assert 'retest_claims' in insp.get_table_names()
assert 'claim_id' in {c['name'] for c in insp.get_columns('shrinkage_samples')}
with db_module.Session() as s:
    assert s.query(RetestStation).count() == 2
print('✓ 全新库：新表、claim_id、两个工位就位，重复执行幂等')

# 场景 B：旧结构库（无新表、shrinkage_samples 无 claim_id）
old = create_engine('sqlite://')
with old.begin() as conn:
    conn.execute(text('CREATE TABLE shrinkage_samples (id INTEGER PRIMARY KEY, run_id INTEGER, specimen VARCHAR(32), stage VARCHAR(8), thickness_mm FLOAT, is_retest BOOLEAN, technician VARCHAR(32), recorded_at DATETIME)'))
db_module.engine = old
db_module.Session = sessionmaker(bind=old)
Base.metadata.create_all(old)  # 只补齐缺失的表
add_missing_columns()
ensure_stations()
insp = inspect(old)
assert {'retest_stations', 'retest_claims'} <= set(insp.get_table_names())
assert 'claim_id' in {c['name'] for c in insp.get_columns('shrinkage_samples')}
add_missing_columns()  # 再跑一次不应报错/重复加列
print('✓ 旧结构库：自动补 claim_id 与工位表，重复执行不报错')
print('迁移验证全部通过')
