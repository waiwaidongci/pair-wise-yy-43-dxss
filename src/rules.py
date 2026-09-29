from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
SEGMENT_ENTITY='岸线段'
SEGMENT_STATUSES=['open', 'claimed', 'completed', 'reinspection', 'reoiled']
SEGMENT_STATUS_LABELS={'open': '待领取', 'claimed': '清理中', 'completed': '已完成', 'reinspection': '待复检', 'reoiled': '复油待清理'}
SENSITIVITY_LABELS={'low': '低敏感', 'moderate': '中敏感', 'high': '高敏感', 'critical': '生态极敏感'}
# 完成复测允许的残余油膜厚度上限（微米）：敏感等级越高阈值越严
OIL_FILM_THRESHOLDS_UM={'low': 100.0, 'moderate': 50.0, 'high': 20.0, 'critical': 5.0}
SEGMENT_REGISTER_ROLES=set(['observer', 'response_commander', 'operations'])
SEGMENT_CLAIM_ROLES=set(['operations', 'response_commander'])
SEGMENT_COMPLETE_ROLES=set(['operations', 'response_commander'])
SEGMENT_REOIL_ROLES=set(['observer', 'response_commander', 'operations'])
SEGMENT_REVIEW_ROLES=set(['response_commander'])
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
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def film_threshold(sensitivity):
    if sensitivity not in OIL_FILM_THRESHOLDS_UM: raise ValidationError("未知敏感等级")
    return OIL_FILM_THRESHOLDS_UM[sensitivity]
def complete_outcome(film_thickness_um,cleaned_quantity,sensitivity):
    """资料不全或实测油膜超阈值 -> 待复检，否则完成。"""
    if film_thickness_um is None or cleaned_quantity is None:
        return 'reinspection'
    return 'completed' if film_thickness_um<=film_threshold(sensitivity) else 'reinspection'
def segment_closure_blockers(segments):
    """未完成或复油（含待复检、清理中、待领取）的段阻塞事件关闭。"""
    blockers=[]
    for seg in segments:
        status=seg['status']
        code=seg.get('code') or f"#{seg['id']}"
        if status=='reinspection': blockers.append(f"岸线段{code}待复检")
        elif status=='reoiled': blockers.append(f"岸线段{code}返油后需重新清理")
        elif status=='claimed': blockers.append(f"岸线段{code}清理作业未结束")
        elif status=='open': blockers.append(f"岸线段{code}尚未领取清理")
    return blockers
