"""晚高峰备用柜调拨决策。

同批站点的记录用同一约定时长比较滞留比例。观察在该时长前结束且尚未取件的
柜件结果未知，不假定其未取：枚举未知柜件的所有可能取件结果，若同一站始终
独占第一则调拨给它；第一名可能换站或并列则留库。对账发现坏行一律留库，
即使坏行所在站点整组都没进计算。
"""
from fractions import Fraction


def span(observations, horizon):
    """按约定时长划分柜件：返回（已知滞留数， 结果未知数）。"""
    known = unknown = 0
    for age, picked in observations:
        if picked:
            known += age > horizon
        elif age >= horizon:
            known += 1
        else:
            unknown += 1
    return known, unknown


def propose(evaluated, intake):
    spans = []
    horizons = set()
    for case, payload in evaluated:
        if payload is None:
            continue
        observations, horizon, _ = payload
        known, unknown = span(observations, horizon)
        cohort = len(observations)
        horizons.add(horizon)
        spans.append((case['case'], cohort, known, unknown,
                      Fraction(known, cohort), Fraction(known + unknown, cohort)))
    stations = [dict(case=case, cohort=cohort, known=known, unknown=unknown,
                     low_bp=int(low * 10000), high_bp=int(high * 10000))
                for case, cohort, known, unknown, low, high in spans]
    horizon = horizons.pop() if len(horizons) == 1 else None
    destination = None
    if intake['rejected']:
        reason = 'BAD_ROWS'
    elif not spans:
        reason = 'NO_STATIONS'
    elif horizon is None:
        reason = 'HORIZON_MISMATCH'
    else:
        reason = 'FIRST_UNDECIDED'
        for i, (case, _, _, _, low, _) in enumerate(spans):
            if all(low > spans[j][5] for j in range(len(spans)) if j != i):
                destination, reason = case, 'ROBUST_FIRST'
                break
    return dict(destination=destination, reason=reason, horizon=horizon, stations=stations)
