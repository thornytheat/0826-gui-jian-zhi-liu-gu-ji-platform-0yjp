import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.business import DomainIssue
from app.modules.pipeline import run
from app.review.service import confirm_dispatch, execute
from conftest import csv_data


def cohort(prefix, case, entries, pickups, cutoff, horizon, expected):
    rows, n = [], 0
    for parcel, entry in entries:
        n += 1
        rows.append([f'{prefix}{n:02d}', case, 'arrival', parcel, entry, 0, 0, 0, '到件'])
    for parcel, moment in pickups:
        n += 1
        rows.append([f'{prefix}{n:02d}', case, 'pickup', parcel, 0, moment, 0, 0, '取件'])
    rows.append([f'{prefix}{n+1:02d}', case, 'observation', 0, 0, cutoff, horizon, expected, '观察'])
    return rows


STRONG = cohort('A', '0001', [(1, 0), (2, 0), (3, 0), (4, 0)], [(1, 5), (2, 5)], 12, 10, 5000)
WEAK = cohort('B', '0002', [(1, 0), (2, 0), (3, 0), (4, 0)], [(1, 3), (2, 4), (3, 5)], 12, 10, 2500)
ROBUST = csv_data(STRONG + WEAK)
MIRROR = csv_data(cohort('A', '0001', [(1, 0), (2, 0), (3, 0), (4, 0)], [(1, 3), (2, 4), (3, 5)], 12, 10, 2500)
                  + cohort('B', '0002', [(1, 0), (2, 0), (3, 0), (4, 0)], [(1, 5), (2, 5)], 12, 10, 5000))
FLIP = csv_data(STRONG + cohort('B', '0002', [(1, 0), (2, 0), (3, 0), (4, 5), (5, 6)], [(1, 3), (2, 4)], 12, 10, 6000))
TIE = csv_data(STRONG + cohort('B', '0002', [(1, 0), (2, 0), (3, 0), (4, 5)], [(1, 3), (2, 4)], 12, 10, 5000))
SINGLE = csv_data(cohort('A', '0001', [(1, 0), (2, 4)], [(1, 5)], 12, 10, 5000))
BROKEN = csv_data([['X01', '0001', 'arrival', 1, 4, 0, 0, 0, '到件'],
                   ['X02', '0001', 'pickup', 1, 0, 3, 0, 0, '取件'],
                   ['X03', '0001', 'observation', 0, 0, 8, 4, 0, '观察']])


@pytest.fixture
def night(monkeypatch):
    monkeypatch.setattr('app.review.service.now', lambda: '2026-11-25T22:30:00.000+00:00')
    return '2026-11-25'


def test_robust_first_dispatches_to_station():
    decision = run(ROBUST)['dispatch']
    assert decision['destination'] == '0001' and decision['reason'] == 'ROBUST_FIRST'
    assert decision['horizon'] == 10
    spans = {station['case']: station for station in decision['stations']}
    assert (spans['0001']['known'], spans['0001']['unknown']) == (2, 0)
    assert (spans['0001']['low_bp'], spans['0001']['high_bp']) == (5000, 5000)
    assert (spans['0002']['low_bp'], spans['0002']['high_bp']) == (2500, 2500)


def test_unknown_pickups_can_flip_first_place():
    decision = run(FLIP)['dispatch']
    assert decision['destination'] is None and decision['reason'] == 'FIRST_UNDECIDED'
    spans = {station['case']: station for station in decision['stations']}
    assert (spans['0002']['known'], spans['0002']['unknown']) == (1, 2)
    assert spans['0002']['high_bp'] == 6000


def test_exact_tie_keeps_spare_in_stock():
    decision = run(TIE)['dispatch']
    assert decision['destination'] is None and decision['reason'] == 'FIRST_UNDECIDED'


def test_single_station_is_trivially_first():
    decision = run(SINGLE)['dispatch']
    assert decision['destination'] == '0001' and decision['reason'] == 'ROBUST_FIRST'


def test_bad_rows_keep_spare_even_off_station(dirty):
    decision = run(dirty)['dispatch']
    assert decision['destination'] is None and decision['reason'] == 'BAD_ROWS'
    assert {station['case'] for station in decision['stations']} == {'0001', '0002', '0004'}


def test_horizon_mismatch_keeps_spare(clean):
    decision = run(clean)['dispatch']
    assert decision['destination'] is None and decision['reason'] == 'HORIZON_MISMATCH'
    assert decision['horizon'] is None


def test_no_valid_station_keeps_spare():
    decision = run(BROKEN)['dispatch']
    assert decision['destination'] is None and decision['reason'] == 'NO_STATIONS'
    assert decision['stations'] == []


def test_confirm_records_night_destination(store, night):
    batch = execute(store, ROBUST)
    record = confirm_dispatch(store, batch['id'])
    assert record['night'] == night
    assert record['destination'] == '0001' and record['reason'] == 'ROBUST_FIRST'
    assert record['run_id'] == batch['id']
    assert store.dispatch(night) == record


def test_reconfirm_same_run_is_idempotent(store, night):
    batch = execute(store, ROBUST)
    first = confirm_dispatch(store, batch['id'])
    assert confirm_dispatch(store, batch['id']) == first
    assert len(store.dispatches()) == 1


def test_other_proposal_same_night_conflicts(store, night):
    winner = execute(store, ROBUST)
    other = execute(store, MIRROR)
    confirm_dispatch(store, winner['id'])
    with pytest.raises(DomainIssue, match='NIGHT_DISPATCHED'):
        confirm_dispatch(store, other['id'])
    assert store.dispatch(night)['destination'] == '0001'
    assert len(store.dispatches()) == 1


def test_parallel_confirm_has_single_winner(store, night):
    first, second = execute(store, ROBUST), execute(store, MIRROR)
    barrier = Barrier(2)

    def attempt(batch):
        barrier.wait(3)
        try:
            return confirm_dispatch(store, batch['id'])['destination']
        except DomainIssue as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(attempt, [first, second]))
    winners = [result for result in results if result != 'NIGHT_DISPATCHED']
    assert results.count('NIGHT_DISPATCHED') == 1 and len(winners) == 1
    assert store.dispatch(night)['destination'] == winners[0]
    assert len(store.dispatches()) == 1


def test_confirm_unknown_run(store):
    with pytest.raises(DomainIssue, match='RUN_NOT_FOUND'):
        confirm_dispatch(store, 'missing')


def test_confirm_run_without_proposal(store):
    batch = store.register('f' * 64, {'manifest': dict(created='2026-11-25T22:00:00.000+00:00')})
    with pytest.raises(DomainIssue, match='DISPATCH_NOT_FOUND'):
        confirm_dispatch(store, batch['id'])


def test_http_confirm_and_query_night(client):
    posted = client.post('/api/runs', data={'file': (io.BytesIO(ROBUST), 'robust.csv')})
    assert posted.status_code == 200
    confirmed = client.post(f"/api/runs/{posted.json['id']}/dispatch")
    assert confirmed.status_code == 200
    assert confirmed.json['destination'] == '0001' and confirmed.json['reason'] == 'ROBUST_FIRST'
    night = confirmed.json['night']
    looked_up = client.get(f'/api/dispatches/{night}')
    assert looked_up.status_code == 200 and looked_up.json == confirmed.json
    assert client.get('/api/dispatches').json['items'] == [confirmed.json]


def test_http_second_proposal_same_night_conflicts(client):
    first = client.post('/api/runs', data={'file': (io.BytesIO(ROBUST), 'a.csv')}).json
    second = client.post('/api/runs', data={'file': (io.BytesIO(MIRROR), 'b.csv')}).json
    assert client.post(f"/api/runs/{first['id']}/dispatch").status_code == 200
    conflict = client.post(f"/api/runs/{second['id']}/dispatch")
    assert conflict.status_code == 409 and conflict.json['code'] == 'NIGHT_DISPATCHED'


def test_http_dispatch_not_found(client):
    missing = client.get('/api/dispatches/2099-01-01')
    assert missing.status_code == 404 and missing.json['code'] == 'DISPATCH_NOT_FOUND'
    assert client.post('/api/runs/unknown/dispatch').status_code == 404


def test_dispatch_stage_preview_has_no_side_effect(client, dirty):
    response = client.post('/api/stages/dispatch', data={'sample': 'dirty'})
    assert response.status_code == 200
    assert response.json['dispatch']['reason'] == 'BAD_ROWS'
    assert client.get('/api/runs').json == {'items': []}
    assert client.get('/api/dispatches').json == {'items': []}
