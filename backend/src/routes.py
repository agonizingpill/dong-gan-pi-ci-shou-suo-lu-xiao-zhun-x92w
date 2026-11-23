from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import ledger, retest
from .auth import identity, issue, require, valid_password
from .db import session
from .models import CalibrationRelease, ChamberRun, DryBatch, LabEvent, RecipeLot, Role, User
from .schemas import BatchIn, CurveIn, DecisionIn, Login, RecipeIn, RetestIn, SampleIn

router = APIRouter()
operator = require(Role.OPERATOR)
supervisor = require(Role.SUPERVISOR)
lab_staff = require(Role.OPERATOR, Role.SUPERVISOR)


def commit(db, duplicate):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, duplicate) from exc


def curve_in_batch(db, code, run_id):
    run = db.get(ChamberRun, run_id)
    if not run or run.batch_code != code:
        raise HTTPException(404, '曲线不属于该批次')
    return run


def calibration_context(db, entry_id, lock=False):
    entry = db.get(CalibrationRelease, entry_id)
    if entry is None:
        raise HTTPException(404, '没有找到该校准单')
    run = db.get(ChamberRun, entry.run_id)
    batch = ledger.locked_batch(db, run.batch_code) if lock else db.get(DryBatch, run.batch_code)
    if lock:
        db.refresh(entry, with_for_update=True)
    return batch, run, entry


@router.post('/session')
def login(payload: Login, db: Session = Depends(session)):
    user = db.get(User, payload.username)
    if user is None or not valid_password(payload.password, user.password_hash):
        raise HTTPException(401, '账号或密码错误')
    ledger.record(db, user.username, 'session.login', user.role.value)
    db.commit()
    return {'token': issue(user), 'username': user.username, 'role': user.role}


@router.get('/session')
def current_session(user=Depends(identity)):
    return {'username': user.username, 'role': user.role}


@router.get('/recipes', dependencies=[Depends(identity)])
def recipes(db: Session = Depends(session)):
    return [ledger.row(recipe) for recipe in db.scalars(select(RecipeLot).order_by(RecipeLot.code))]


@router.post('/recipes', status_code=201)
def create_recipe(payload: RecipeIn, db: Session = Depends(session), user=Depends(supervisor)):
    recipe = RecipeLot(**payload.model_dump())
    db.add(recipe)
    ledger.record(db, user.username, 'recipe.create', recipe.code)
    commit(db, '配方版本已存在，请分配新版本号')
    return ledger.row(recipe)


@router.get('/batches', dependencies=[Depends(identity)])
def batches(db: Session = Depends(session)):
    return [ledger.row(batch) for batch in db.scalars(select(DryBatch).order_by(DryBatch.code))]


@router.post('/batches', status_code=201)
def create_batch(payload: BatchIn, db: Session = Depends(session), user=Depends(operator)):
    if db.get(RecipeLot, payload.recipe_code) is None:
        raise HTTPException(404, '请选用已建档的配方版本')
    batch = DryBatch(**payload.model_dump())
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, '冻干批号已在实验室登记') from exc
    ledger.record(db, user.username, 'batch.register', f'{batch.material_lot} · {batch.dryer}', batch.code)
    db.commit()
    return ledger.row(batch)


@router.get('/batches/{code}', dependencies=[Depends(identity)])
def batch_detail(code: str, db: Session = Depends(session)):
    batch = db.get(DryBatch, code)
    if batch is None:
        raise HTTPException(404, '没有找到该冻干批次')
    runs = db.scalars(select(ChamberRun).filter_by(batch_code=code).order_by(ChamberRun.id.desc())).all()
    active = runs[0] if runs else None
    samples = ledger.samples_for(db, active.id) if active else []
    entry = ledger.release_for(db, active.id) if active else None
    readiness = None
    try:
        ledger.paired_measurements(db, ledger.training_run(db, batch))
    except ValueError as exc:
        readiness = str(exc)
    return {'batch': ledger.row(batch), 'recipe': ledger.row(db.get(RecipeLot, batch.recipe_code)),
            'runs': [ledger.row(run) for run in runs], 'samples': [ledger.row(sample) for sample in samples],
            'calibration': ledger.row(entry) if entry else None, 'readiness_issue': readiness}


@router.post('/batches/{code}/curves', status_code=201)
def import_curve(code: str, payload: CurveIn, db: Session = Depends(session), user=Depends(operator)):
    batch = ledger.locked_batch(db, code)
    run = ledger.add_curve(db, batch, payload.model_dump(mode='json'), user.username)
    db.commit()
    return ledger.row(run)


@router.post('/batches/{code}/curves/{run_id}/freeze')
def freeze(code: str, run_id: int, db: Session = Depends(session), user=Depends(operator)):
    batch = ledger.locked_batch(db, code)
    run = curve_in_batch(db, code, run_id)
    ledger.freeze_curve(db, batch, run, user.username)
    db.commit()
    return ledger.row(run)


@router.post('/batches/{code}/curves/{run_id}/samples', status_code=201)
def record_sample(code: str, run_id: int, payload: SampleIn, db: Session = Depends(session), user=Depends(operator)):
    batch = ledger.locked_batch(db, code)
    run = curve_in_batch(db, code, run_id)
    sample = ledger.add_sample(db, batch, run, payload.model_dump(), user.username)
    db.commit()
    return ledger.row(sample)


@router.post('/batches/{code}/archive')
def archive(code: str, payload: DecisionIn, db: Session = Depends(session), user=Depends(supervisor)):
    batch = ledger.locked_batch(db, code)
    ledger.archive_reference(db, batch, user.username, payload.note)
    db.commit()
    return ledger.row(batch)


@router.post('/batches/{code}/calibration', status_code=202)
def request_calibration(code: str, db: Session = Depends(session), user=Depends(lab_staff)):
    batch = ledger.locked_batch(db, code)
    entry = ledger.queue_fit(db, batch, user.username)
    db.commit()
    ledger.wake_worker(entry.id)
    return ledger.row(entry)


@router.get('/calibrations', dependencies=[Depends(identity)])
def register(db: Session = Depends(session)):
    rows = db.execute(select(CalibrationRelease, ChamberRun).join(ChamberRun)
                      .order_by(CalibrationRelease.id.desc())).all()
    return [{'id': entry.id, 'batch_code': run.batch_code, 'source_name': run.source_name,
             'state': entry.state, 'prediction_pct': entry.prediction_pct, 'requested_by': entry.requested_by}
            for entry, run in rows]


@router.get('/calibrations/{entry_id}', dependencies=[Depends(identity)])
def detail(entry_id: int, db: Session = Depends(session)):
    batch, run, entry = calibration_context(db, entry_id)
    events = db.scalars(select(LabEvent).filter_by(batch_code=batch.code).order_by(LabEvent.id.desc()).limit(30))
    claim = retest.active_claim_for(db, entry_id)
    return {'batch': ledger.row(batch), 'curve': ledger.row(run), 'entry': ledger.row(entry),
            'retest': [ledger.row(sample) for sample in ledger.samples_for(db, run.id, retest=True)],
            'queue_basis': retest.basis_breakdown(entry) if entry.prediction_pct is not None else None,
            'active_claim': retest.claim_payload(db, claim) if claim else None,
            'events': [ledger.row(event) for event in events]}


@router.post('/calibrations/{entry_id}/retry', status_code=202)
def retry(entry_id: int, db: Session = Depends(session), user=Depends(lab_staff)):
    batch, _, entry = calibration_context(db, entry_id, lock=True)
    ledger.retry_fit(db, batch, entry, user.username)
    db.commit()
    ledger.wake_worker(entry.id)
    return ledger.row(entry)


@router.get('/retest-board', dependencies=[Depends(identity)])
def retest_board(db: Session = Depends(session)):
    return retest.board(db)


def release_batch_code(db, entry_id):
    entry = db.get(CalibrationRelease, entry_id)
    if entry is None:
        raise HTTPException(404, '没有找到该校准单')
    return db.get(ChamberRun, entry.run_id).batch_code


def lock_claim_batch(db, claim_id):
    claim = db.get(retest.RetestClaim, claim_id)
    if claim is None:
        raise HTTPException(404, '没有找到该领单')
    entry = db.get(CalibrationRelease, claim.release_id)
    code = db.get(ChamberRun, entry.run_id).batch_code
    return ledger.locked_batch(db, code), claim


@router.post('/calibrations/{entry_id}/claim')
def claim(entry_id: int, db: Session = Depends(session), user=Depends(lab_staff)):
    ledger.locked_batch(db, release_batch_code(db, entry_id))
    claim, reused = retest.claim_station(db, entry_id, user.username)
    db.commit()
    return {'claim': retest.claim_payload(db, claim), 'reused': reused}


@router.put('/claims/{claim_id}/draft')
def save_draft(claim_id: int, payload: RetestIn, db: Session = Depends(session), user=Depends(lab_staff)):
    _, claim = lock_claim_batch(db, claim_id)
    db.refresh(claim, with_for_update=True)
    retest.save_draft(db, claim, payload, user.username)
    db.commit()
    return retest.claim_payload(db, claim)


@router.post('/claims/{claim_id}/return')
def return_claim(claim_id: int, payload: DecisionIn, db: Session = Depends(session), user=Depends(lab_staff)):
    _, claim = lock_claim_batch(db, claim_id)
    db.refresh(claim, with_for_update=True)
    retest.return_claim(db, claim, user.username, payload.note)
    db.commit()
    return retest.claim_payload(db, claim)


@router.post('/calibrations/{entry_id}/retest')
def record_retest(entry_id: int, payload: RetestIn, db: Session = Depends(session), user=Depends(operator)):
    batch, _, entry = calibration_context(db, entry_id, lock=True)
    claim = retest.get_active_claim(db, entry_id, user.username)
    entry, claim = retest.book_retest(db, claim, payload, user.username)
    db.commit()
    return {'entry': ledger.row(entry), 'claim': retest.claim_payload(db, claim)}


@router.post('/calibrations/{entry_id}/release')
def sign(entry_id: int, payload: DecisionIn, db: Session = Depends(session), user=Depends(supervisor)):
    batch, run, entry = calibration_context(db, entry_id, lock=True)
    ledger.sign_release(db, batch, run, entry, user.username, payload.note)
    db.commit()
    return ledger.row(entry)
