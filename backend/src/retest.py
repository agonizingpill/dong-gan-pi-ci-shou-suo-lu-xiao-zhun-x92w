"""复测工位排队与领单领域规则。

实验室固定两个复测工位。出分后的校准单按
``预测到最近窗边距离 − 留一误差（LOO RMSE）`` 升序排队，新出分单实时插队；
已领单占住工位直到复测入账或交班退回。
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import utcnow
from .ledger import blocked, inside, record
from .models import (
    CalibrationRelease, ChamberRun, ClaimStatus, DryBatch, ReleaseState, RetestClaim,
    RetestStation, ShrinkageSample, Stage, STATION_COUNT,
)

RETEST_STATES = (ReleaseState.AWAITING, ReleaseState.RETEST)


def window_margin(prediction, window):
    """预测点到最近窗边的绝对距离（百分点）。"""
    low, high = window
    return min(abs(prediction - low), abs(prediction - high))


def queue_basis(entry):
    """排队依据：最近窗边距离减去留一误差，越小越靠前。"""
    margin = window_margin(entry.prediction_pct, entry.snapshot['window'])
    loo_rmse = (entry.result or {}).get('loo_rmse') or 0.0
    return round(margin - loo_rmse, 6)


def basis_breakdown(entry):
    window = entry.snapshot['window']
    margin = window_margin(entry.prediction_pct, window)
    loo_rmse = (entry.result or {}).get('loo_rmse') or 0.0
    return {
        'prediction_pct': entry.prediction_pct,
        'window_low': window[0], 'window_high': window[1],
        'nearest_edge_margin': round(margin, 6),
        'loo_rmse': round(loo_rmse, 6),
        'queue_key': round(margin - loo_rmse, 6),
    }


def eligible_entries(db):
    """出分待复测且没有活跃领单的校准单，按排队依据升序。"""
    active = select(RetestClaim.release_id).where(RetestClaim.status == ClaimStatus.ACTIVE)
    entries = db.scalars(
        select(CalibrationRelease)
        .where(CalibrationRelease.state.in_(RETEST_STATES))
        .where(CalibrationRelease.prediction_pct.is_not(None))
        .where(CalibrationRelease.id.not_in(active))
    ).all()
    return sorted(entries, key=lambda entry: (queue_basis(entry), entry.touched_at, entry.id))


def active_claim_for(db, release_id):
    return db.scalar(select(RetestClaim).where(
        RetestClaim.release_id == release_id, RetestClaim.status == ClaimStatus.ACTIVE))


def active_claims(db):
    return db.scalars(select(RetestClaim).where(RetestClaim.status == ClaimStatus.ACTIVE)
                      .order_by(RetestClaim.station_id)).all()


def _batch_for(db, entry):
    run = db.get(ChamberRun, entry.run_id)
    return db.get(DryBatch, run.batch_code)


def claim_station(db, entry_id, actor):
    """按队列头名领单；重试同一单返回原领单，不新增工位占用。"""
    stations = db.scalars(select(RetestStation).order_by(RetestStation.id).with_for_update()).all()
    entry = db.get(CalibrationRelease, entry_id, with_for_update=True)
    if entry is None:
        raise HTTPException(404, '没有找到该校准单')
    if entry.state not in RETEST_STATES:
        blocked(db, actor, 'retest.claim', '当前校准单状态不进入复测工位排队', _batch_for(db, entry).code)
    existing = active_claim_for(db, entry_id)
    if existing is not None:
        existing.attempts += 1
        existing.claimed_at = utcnow()
        record(db, actor, 'retest.claim.retry',
               f'工位 {existing.station_id} 领单重试，占用不变（第 {existing.attempts} 次）',
               _batch_for(db, entry).code)
        db.flush()
        return existing, True
    head = eligible_entries(db)
    if not head or head[0].id != entry.id:
        blocked(db, actor, 'retest.claim', '只能领取队列头名单，新出分单可按排队依据插队',
                _batch_for(db, entry).code)
    occupied = {claim.station_id for claim in active_claims(db)}
    free = next((station for station in stations if station.id not in occupied), None)
    if free is None:
        blocked(db, actor, 'retest.claim', '两个复测工位均被占用，已领单完成或交班退回后再试',
                _batch_for(db, entry).code)
    claim = RetestClaim(release_id=entry.id, station_id=free.id, claimed_by=actor,
                        release_slot=entry.id, station_slot=free.id)
    db.add(claim)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        blocked(db, actor, 'retest.claim', '工位刚被其他班次领取，请刷新排队后重试',
                _batch_for(db, entry).code)
    record(db, actor, 'retest.claim', f'工位 {free.id}（{free.name}）领取校准单 #{entry.id}',
           _batch_for(db, entry).code)
    return claim, False


def save_draft(db, claim, payload, actor):
    """保存未完成的复测草稿读数；草稿不会生成正式测量、不能入账。"""
    entry = db.get(CalibrationRelease, claim.release_id, with_for_update=True)
    claim.draft = payload.model_dump(mode='json')
    claim.claimed_at = utcnow()
    record(db, actor, 'retest.draft',
           f"工位 {claim.station_id} 草稿 {payload.specimen} · {payload.before_mm:g}/{payload.after_mm:g} mm",
           _batch_for(db, entry).code)
    db.flush()
    return claim


def _book_samples(db, entry, payload, claim, actor):
    run = db.get(ChamberRun, entry.run_id)
    if any(item.specimen == payload.specimen for item in
           db.scalars(select(ShrinkageSample).filter_by(run_id=run.id)).all()):
        blocked(db, actor, 'retest.record', '复测须取独立试片，试片编号不能与首测重复',
                _batch_for(db, entry).code)
    db.add_all([
        ShrinkageSample(run_id=run.id, technician=actor, specimen=payload.specimen, stage=stage,
                        thickness_mm=reading, is_retest=True, claim_id=claim.id)
        for stage, reading in [(Stage.PRE, payload.before_mm), (Stage.POST, payload.after_mm)]
    ])


def book_retest(db, claim, payload, actor):
    """持活跃领单完成复测入账，并腾退工位。"""
    entry = db.get(CalibrationRelease, claim.release_id, with_for_update=True)
    if claim.status != ClaimStatus.ACTIVE:
        blocked(db, actor, 'retest.record', '该领单已结案，交班退回后的旧读数不能入账',
                _batch_for(db, entry).code)
    if entry.state not in RETEST_STATES:
        blocked(db, actor, 'retest.record', '当前校准状态不接受复测', _batch_for(db, entry).code)
    _book_samples(db, entry, payload, claim, actor)
    entry.recheck_pct = round(100 * (1 - payload.after_mm / payload.before_mm), 6)
    entry.recheck_note = payload.note
    acceptable = entry.state == ReleaseState.AWAITING and inside(entry.recheck_pct, entry.snapshot['window'])
    entry.state = ReleaseState.VERIFIED if acceptable else ReleaseState.HELD
    entry.touched_at = utcnow()
    claim.status = ClaimStatus.COMPLETED
    claim.draft = None
    claim.finished_at = utcnow()
    claim.release_slot = -claim.id
    claim.station_slot = -claim.id
    action = 'retest.verified' if acceptable else 'retest.held'
    record(db, actor, action,
           f'工位 {claim.station_id} · {payload.specimen} · {entry.recheck_pct:.2f}% · {payload.note}',
           _batch_for(db, entry).code)
    db.flush()
    return entry, claim


def return_claim(db, claim, actor, note):
    """交班退回未测单：工位归还队列，旧领单上的草稿读数不能入账。"""
    entry = db.get(CalibrationRelease, claim.release_id, with_for_update=True)
    if claim.status != ClaimStatus.ACTIVE:
        blocked(db, actor, 'retest.return', '只有进行中的领单可以退回队列', _batch_for(db, entry).code)
    stale = claim.draft
    claim.status = ClaimStatus.RETURNED
    claim.finished_at = utcnow()
    claim.draft = None
    claim.release_slot = -claim.id
    claim.station_slot = -claim.id
    detail = f'工位 {claim.station_id} 退回校准单 #{entry.id} · {note}'
    if stale:
        detail += f"；草稿 {stale.get('specimen')} 的读数作废，不入台账"
    record(db, actor, 'retest.return', detail, _batch_for(db, entry).code)
    db.flush()
    return claim


def claim_payload(db, claim):
    station = db.get(RetestStation, claim.station_id)
    entry = db.get(CalibrationRelease, claim.release_id)
    run = db.get(ChamberRun, entry.run_id)
    batch = db.get(DryBatch, run.batch_code)
    return {
        'id': claim.id, 'station_id': claim.station_id, 'station_name': station.name,
        'release_id': entry.id, 'batch_code': batch.code, 'state': claim.status,
        'claimed_by': claim.claimed_by, 'attempts': claim.attempts, 'draft': claim.draft,
        'claimed_at': claim.claimed_at, 'finished_at': claim.finished_at,
        'calibration_state': entry.state,
        **basis_breakdown(entry),
    }


def board(db):
    """复测工位页面总览：工位占用、排队依据、近期领单。"""
    stations = []
    claims_by_station = {claim.station_id: claim for claim in active_claims(db)}
    for station in db.scalars(select(RetestStation).order_by(RetestStation.id)).all():
        claim = claims_by_station.get(station.id)
        stations.append({'id': station.id, 'name': station.name,
                         'occupied': claim is not None,
                         'claim': claim_payload(db, claim) if claim else None})
    queue = []
    for position, entry in enumerate(eligible_entries(db), start=1):
        run = db.get(ChamberRun, entry.run_id)
        queue.append({'position': position, 'release_id': entry.id,
                      'batch_code': run.batch_code, 'calibration_state': entry.state,
                      **basis_breakdown(entry)})
    recent = db.scalars(select(RetestClaim).order_by(RetestClaim.id.desc()).limit(10)).all()
    return {
        'station_count': STATION_COUNT,
        'stations': stations,
        'queue': queue,
        'recent_claims': [claim_payload(db, claim) for claim in recent],
        'rule': '按预测到最近窗边距离减去留一误差升序排队；新单可插队，已领单完成或交班退回后才腾出工位。',
    }


def get_active_claim(db, entry_id, actor):
    """取出校准单对应的活跃领单并加行锁；未领单则拒绝入账。"""
    entry = db.get(CalibrationRelease, entry_id)
    if entry is None:
        raise HTTPException(404, '没有找到该校准单')
    claim = active_claim_for(db, entry_id)
    if claim is None:
        run = db.get(ChamberRun, entry.run_id)
        blocked(db, actor, 'retest.record', '该单尚未领取复测工位，请先在复测工位页领单', run.batch_code)
    db.refresh(claim, with_for_update=True)
    return claim
