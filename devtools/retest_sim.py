"""复测工位排队的端到端规则验证（独立脚本，使用 SQLite 内存库）。

运行：python3 -I /workspace/devtools/retest_sim.py
不依赖 MySQL/Redis/sklearn，直接构造已出分校准单驱动 HTTP API。
"""
import sys
from datetime import datetime

sys.path.insert(0, '/tmp/pylibs')
sys.path.insert(0, '/workspace/backend')

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.db as db_module
from src.app import app
from src.auth import issue
from src.models import (
    Base, CalibrationRelease, ClaimStatus, DryBatch, RecipeLot, RetestClaim, RetestStation,
    ReleaseState, RunState, ChamberRun, ShrinkageSample, Stage, User,
)
from src.auth import password_hash

engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
TestSession = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)
db_module.engine = engine
db_module.Session = TestSession


def override_session():
    with TestSession() as session:
        yield session


app.dependency_overrides[db_module.session] = override_session
client = TestClient(app)

PASS = 0
FAIL = 0


def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} :: {detail}')


def token(username):
    with TestSession() as session:
        return {'Authorization': f'Bearer {issue(session.get(User, username))}'}


def seed_identity():
    with TestSession() as session:
        for role in ('operator', 'supervisor', 'viewer'):
            if session.get(User, role) is None:
                session.add(User(username=role, password_hash=password_hash('x'), role=role))
        session.add_all([
            RetestStation(id=1, name='复测工位 1'), RetestStation(id=2, name='复测工位 2'),
            RecipeLot(code='AP-1', product='青苹果', solids_pct=14.2, nominal_mm=8.0,
                      window_low=9.0, window_high=16.0),
        ])
        session.commit()


def make_entry(code, state, prediction, loo, *, window=(9.0, 16.0)):
    with TestSession() as session:
        batch = DryBatch(code=code, recipe_code='AP-1', dryer='FDX-02', material_lot=f'M-{code}',
                         feed_mm=8.0)
        session.add(batch)
        session.flush()
        run = ChamberRun(batch_code=code, source_name=f'{code}.csv', state=RunState.FROZEN,
                         points=[{'minute': 1, 'phase': 'freeze', 'shelf_c': 1.0, 'pressure_pa': 10.0}],
                         checksum='abc')
        session.add(run)
        session.flush()
        entry = CalibrationRelease(
            run_id=run.id, requested_by='operator', input_hash='h',
            snapshot={'batch': code, 'recipe': 'AP-1', 'window': list(window), 'training': []},
            state=state, prediction_pct=prediction,
            result={'prediction_pct': prediction, 'loo_rmse': loo}, touched_at=datetime(2026, 11, 22, 8, 0))
        session.add(entry)
        session.commit()
        return entry.id


def board():
    return client.get('/retest-board', headers=token('operator')).json()


def find_queue(board_json, release_id):
    return next(item for item in board_json['queue'] if item['release_id'] == release_id)


print('== 1. 排队依据：最近窗边距离 − 留一误差，升序，新单可插队 ==')
seed_identity()
# 窗 9–16：预测 12 → 距离 min(3,4)=3；预测 15.5 → 距离 0.5；超窗 17 → 距离 1
id_a = make_entry('FD-A', ReleaseState.AWAITING, 12.0, 0.4)   # key 2.6
id_b = make_entry('FD-B', ReleaseState.RETEST, 17.0, 0.2)     # key 0.8
id_c = make_entry('FD-C', ReleaseState.AWAITING, 15.5, 0.1)   # key 0.4 → 队首
data = board()
order = [item['release_id'] for item in data['queue']]
check('三单全部入队', len(data['queue']) == 3, str(order))
check('按依据升序 C,B,A', order == [id_c, id_b, id_a], str(order))
head = find_queue(data, id_c)
check('依据明细 0.5-0.1=0.4', abs(head['queue_key'] - 0.4) < 1e-9, str(head))
check('工位初始全空闲', all(not s['occupied'] for s in data['stations']))

print('== 2. 只能领队首；两工位满后拒绝；一单一工位 ==')
r = client.post(f'/calibrations/{id_b}/claim', headers=token('operator'))
check('非队首领单被 409', r.status_code == 409, r.text)
r1 = client.post(f'/calibrations/{id_c}/claim', headers=token('operator'))
check('队首领单成功', r1.status_code == 200, r1.text)
claim_c = r1.json()['claim']
check('占用 1 号工位', claim_c['station_id'] == 1, str(claim_c))
r2 = client.post(f'/calibrations/{id_b}/claim', headers=token('supervisor'))
check('第二班领第二单成功', r2.status_code == 200 and r2.json()['claim']['station_id'] == 2, r2.text)
r3 = client.post(f'/calibrations/{id_a}/claim', headers=token('operator'))
check('两工位满再领被 409', r3.status_code == 409, r3.text)
data = board()
check('队列只剩 A', [i['release_id'] for i in data['queue']] == [id_a], str(data['queue']))
check('两个工位均占用', all(s['occupied'] for s in data['stations']))

print('== 3. 领单重试不新增占用 ==')
r = client.post(f'/calibrations/{id_c}/claim', headers=token('operator'))
body = r.json()
check('重试返回原领单', r.status_code == 200 and body['reused'] is True and body['claim']['id'] == claim_c['id'], r.text)
check('重试计数 +1 且工位不变', body['claim']['attempts'] == 2 and body['claim']['station_id'] == 1, str(body))
with TestSession() as session:
    check('领单表仍只有 2 条活跃单',
          session.query(RetestClaim).filter_by(status=ClaimStatus.ACTIVE).count() == 2)

print('== 4. 未领单不能入账；草稿不入账；入账后腾位并写正式测量 ==')
r = client.post(f'/calibrations/{id_a}/retest', headers=token('operator'),
                json={'specimen': 'TRAY-X', 'before_mm': 8.0, 'after_mm': 7.0, 'note': '无工位尝试'})
check('无活跃领单入账被 409', r.status_code == 409, r.text)
draft = {'specimen': 'TRAY-D1', 'before_mm': 8.0, 'after_mm': 7.1, 'note': '交班草稿'}
r = client.put(f'/claims/{claim_c["id"]}/draft', headers=token('operator'), json=draft)
check('草稿可保存', r.status_code == 200 and r.json()['draft']['specimen'] == 'TRAY-D1', r.text)
with TestSession() as session:
    check('草稿没有生成正式测量', session.query(ShrinkageSample).filter_by(is_retest=True).count() == 0)
booked = {'specimen': 'TRAY-R1', 'before_mm': 8.0, 'after_mm': 7.08, 'note': '窗内独立试片复测'}
r = client.post(f'/calibrations/{id_c}/retest', headers=token('operator'), json=booked)
check('持活跃领单入账成功', r.status_code == 200, r.text)
check('窗内复测 → verified', r.json()['entry']['state'] == 'verified', r.text)
check('领单状态 completed', r.json()['claim']['state'] == 'completed', r.text)
data = board()
station1 = next(s for s in data['stations'] if s['id'] == 1)
check('入账后 1 号工位腾退', not station1['occupied'])
with TestSession() as session:
    samples = session.scalars(select(ShrinkageSample).filter_by(is_retest=True)).all()
    check('正式复测读数 2 条且绑定领单', len(samples) == 2 and all(s.claim_id == claim_c['id'] for s in samples),
          str([(s.specimen, s.claim_id) for s in samples]))
    done = session.get(RetestClaim, claim_c['id'])
    check('结案槽位移出唯一约束', done.release_slot < 0 and done.station_slot < 0)

print('== 5. 新出分单按依据插队到队首 ==')
id_d = make_entry('FD-D', ReleaseState.RETEST, 16.2, 0.05)  # 距离 0.2，key 0.15
data = board()
check('新单 D 插队到 A 前面', [i['release_id'] for i in data['queue']][:2] == [id_d, id_a],
      str([(i['release_id'], i['queue_key']) for i in data['queue']]))

print('== 6. 交班退回：草稿作废、不入账、单回队列、可重新领单 ==')
claim_b = r2.json()['claim']
client.put(f'/claims/{claim_b["id"]}/draft', headers=token('supervisor'),
           json={'specimen': 'TRAY-OLD', 'before_mm': 8.0, 'after_mm': 6.5, 'note': '旧班次草稿'})
r = client.post(f'/claims/{claim_b["id"]}/return', headers=token('supervisor'),
                json={'note': '早班交班退回未测单'})
check('交班退回成功', r.status_code == 200 and r.json()['state'] == 'returned', r.text)
check('退回后草稿清空', r.json()['draft'] is None)
with TestSession() as session:
    check('旧草稿读数未入测量表',
          session.query(ShrinkageSample).filter_by(is_retest=True, specimen='TRAY-OLD').count() == 0)
    stale = session.get(RetestClaim, claim_b['id'])
    check('旧领单不能再入账（已退回）', stale.status == ClaimStatus.RETURNED)
data = board()
queued_ids = [i['release_id'] for i in data['queue']]
check('退回单重新出现在队列', id_b in queued_ids, str(queued_ids))
# 1 号工位已空，领回 B（此时 D 仍是队首，先领 D 再领 B）
client.post(f'/calibrations/{id_d}/claim', headers=token('operator'))
r = client.post(f'/calibrations/{id_b}/claim', headers=token('operator'))
check('退回单可重新领单且生成新领单', r.status_code == 200 and r.json()['reused'] is False, r.text)
new_claim = r.json()['claim']
check('新领单不带旧草稿', new_claim['draft'] is None and new_claim['id'] != claim_b['id'])

print('== 7. 超窗复录入账 → held；DB 唯一约束保证一工位一单 ==')
r = client.post(f'/calibrations/{id_b}/retest', headers=token('operator'),
                json={'specimen': 'TRAY-R2', 'before_mm': 8.0, 'after_mm': 6.2, 'note': '超窗留置复测'})
check('超窗状态复测入账 → held', r.status_code == 200 and r.json()['entry']['state'] == 'held', r.text)
data = board()
check('入账后 B 的 2 号工位腾退（D 单仍在 1 号）',
      next(s for s in data['stations'] if s['id'] == 2)['occupied'] is False
      and next(s for s in data['stations'] if s['id'] == 1)['occupied'] is True)
from sqlalchemy.exc import IntegrityError
with TestSession() as session:
    # D 单已有活跃领单，再插一条同单活跃领单必须被唯一约束拒绝
    session.add(RetestClaim(release_id=id_d, station_id=2, claimed_by='x',
                            release_slot=id_d, station_slot=2))
    try:
        session.commit()
        conflict = False
    except IntegrityError:
        session.rollback()
        conflict = True
    check('同一校准单重复活跃领单触发唯一约束', conflict)

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
