from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
# 岸线段台账：敏感等级、清理状态与实测油膜验收阈值
SEGMENT_SENSITIVITIES=['high', 'medium', 'low']
SEGMENT_STATUSES=['open', 'occupied', 'completed', 'pending_recheck', 'reopened']
OIL_FILM_THRESHOLD=1.0
SEGMENT_STATUS_LABELS={'open': '待清理', 'occupied': '占用中', 'completed': '已完成', 'pending_recheck': '待复检', 'reopened': '复油重开'}
SENSITIVITY_LABELS={'high': '高敏感', 'medium': '中敏感', 'low': '低敏感'}
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records=0,unfinished_segments=0,reoiled_segments=0):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    if unfinished_segments>0: blockers.append("仍有未完成岸线段")
    if reoiled_segments>0: blockers.append("仍有复油岸线段")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def normalize_sensitivity(value):
    if value not in SEGMENT_SENSITIVITIES: raise ValidationError("敏感等级不在允许范围内")
    return value
def evaluate_segment_completion(thickness,cleanup_amount):
    # 资料不全或缺实测油膜厚度，或厚度超过验收阈值，均转待复检
    if thickness is None or cleanup_amount is None: return 'pending_recheck'
    if thickness>OIL_FILM_THRESHOLD: return 'pending_recheck'
    return 'completed'
def segment_can_claim(status): return status in ('open','reopened')
def segment_can_complete(status): return status=='occupied'
def segment_can_reinspect(status): return status=='pending_recheck'
def segment_can_reoil(status): return status in ('completed','pending_recheck')
