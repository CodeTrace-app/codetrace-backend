"""분석 — PR 변경 판별 (이슈 #24).

데이터 담당의 웹훅(#29)이 여기 함수를 호출한다. 인터페이스는 #24 코멘트에서 합의했다.
"""

from src.analysis.pr_changes import ChangedFile, Impacted, Warning, detect_changes

__all__ = ["ChangedFile", "Impacted", "Warning", "detect_changes"]
