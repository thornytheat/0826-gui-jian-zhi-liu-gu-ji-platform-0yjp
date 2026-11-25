from app.business import DomainIssue
from app.intake.service import inspect
from app.modules.calculate import compute
from app.modules.dispatch import propose
from app.modules.grouping import case_groups
from app.modules.policy import policy
from app.modules.prepare import prepare
from app.modules.reconcile import balance


def evaluate(rows, stage):
    case = {'case':rows[0]['case'],'records':[r['record'] for r in rows], 'status':'READY'}
    payload = None
    try:
        case['prep'], payload = prepare(rows)
        if stage >= 3:
            case['measure'] = compute(payload)
        if stage >= 4:
            case.update(policy(case['measure']))
    except DomainIssue as error:
        case['status'] = error.code
    return case, payload


def run(data, stage=6):
    intake = inspect(data)
    output = {'intake': {k:v for k,v in intake.items() if k!='rows'}}
    if stage == 1:
        return output
    evaluated = [evaluate(rows, stage) for _, rows in case_groups(intake['rows'])]
    output['cases'] = [case for case, _ in evaluated]
    if stage >= 5:
        output['balance'] = balance(intake,output['cases'])
    if stage >= 6:
        output['dispatch'] = propose(evaluated,intake)
    return output
