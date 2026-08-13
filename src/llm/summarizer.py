"""심볼 배경 요약 생성 (이슈 #28).

설계 원칙: **모델 출력을 신뢰하지 않는다.** 프롬프트로 부탁한 규칙은 서버가 다시 검사하고,
어긴 문장은 버린다. PRD가 꼽은 최대 리스크가 "LLM이 그럴듯한 추측을 지어내는 것"이라서
근거를 댈 수 없는 문장이 화면에 나가면 제품의 존재 이유가 무너진다.

판정 주체도 서버다. 근거가 없으면 LLM을 아예 부르지 않는다.
"모델이 이력 없다고 판단한 것"과 "모델이 실패한 것"은 반드시 구분한다 — 후자를 no_history로
저장하면 근거가 멀쩡한 함수가 영영 "이력 없음"으로 굳는다. 실패는 저장하지 않고 다음에 다시 시도한다.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.db.models import Commit, PullRequest, Repo, Symbol, SymbolEvidence, SymbolSummary
from src.db.query import query
from src.llm import complete
from src.llm.prompts import COMMIT_ITEM, PR_ITEM, RETRY_SUFFIX, SYSTEM_PROMPT, USER_TEMPLATE
from src.llm.provider import LLMUnavailable

logger = logging.getLogger(__name__)

# 근거를 몇 건까지 프롬프트와 응답에 실을지. api-spec §4에 명시된 값이다.
MAX_EVIDENCE = 12
# 요약 문장 하나의 상한. 넘으면 서술이 아니라 나열이 된 것으로 본다.
MAX_SENTENCE_LENGTH = 150
MAX_SUMMARY_LENGTH = 500
SNIPPET_MAX_LINES = 60
SNIPPET_MAX_CHARS = 2000

# 제목 끝의 "(#201)" 같은 표기. 이 레포에서는 실제 PR이 아니라 과거 이력을 흉내 낸 문자열이라
# 모델이 보면 없는 PR을 인용한다. 부탁하는 대신 입력에서 지운다.
_TRAILING_PR_TAG = re.compile(r"\s*\(#\d+\)\s*$")
# "Merge pull request #12 from ..." — 병합 자체는 이 함수가 왜 이런지 설명하지 못한다.
_MERGE_COMMIT = re.compile(r"^Merge (pull request|branch|remote-tracking)\b")
# 추측 표현. 프롬프트로 금지했지만 새어 나오는지 서버가 다시 본다.
# 표현 단위로 적는다. "보통"·"추정"만 넣으면 "보통주", "추정치" 같은 정상 단어가 걸려
# 멀쩡한 문장이 통째로 버려진다.
# 어절 경계로 잡는다. 부분 문자열로 두면 "보통주"·"추정치"가 걸려 멀쩡한 문장이 버려지고,
# 표현만 나열하면 "것 같다"·"위함이다" 같은 흔한 추측이 그대로 새어 나간다.
_SPECULATION = re.compile(
    r"(?<![가-힣])(보통|아마|대체로|짐작)(?![가-힣])"
    r"|것\s*같다|것으로\s*보인다|로\s*보인다|으로\s*보인다"
    # "추정치"·"추정값"은 정상 명사다. 뒤에 오는 글자로 구분한다.
    r"|추정(?![치값])|예상된다|판단된다|가능성이\s*(있|높)"
    r"|일\s*것이다|였을\s*것|하기\s*위함이다|때문일\s*것"
    r"|일반적으로|듯\s*하다|듯하다"
)
_MARKDOWN_START = ("#", "- ", "* ", "```", ">")


@dataclass(frozen=True)
class EvidenceItem:
    """프롬프트에 실을 근거 하나. id는 서버가 붙인 e1, e2...다.

    sha·PR 번호를 id로 쓰지 않는다. 모델이 id를 사실처럼 요약에 옮겨 적는 걸 막는다.
    """

    id: str
    kind: str  # commit | pr
    title: str
    body: str | None
    occurred_at: datetime | None
    number: int | None = None
    review: str | None = None
    # 이 근거를 만든 DB 행(Commit | PullRequest). API가 응답을 조립할 때 쓴다.
    source: object = None


def _clean(text: str | None, limit: int) -> str:
    """프롬프트에 넣을 수 있게 다듬는다.

    **줄바꿈을 공백으로 접는 것이 핵심이다.** 커밋 본문은 고객 레포에서 온 신뢰할 수 없는
    텍스트인데, 줄바꿈을 그대로 통과시키면 본문이 근거 블록의 구조를 흉내 내 가짜 항목을
    끼워 넣을 수 있다. 실재하는 id를 인용하는 가짜 항목은 검증도 통과한다.
    한 줄로 접으면 항목 경계를 위조할 수 없다.

    큰따옴표를 바꾸는 건 소형 모델이 JSON을 뱉을 때 인용부호가 겹쳐 깨지는 걸 줄이기 위함이다.
    """
    if not text:
        return "(없음)"
    folded = " ".join(text.split())
    return folded.replace('"', "'")[:limit]


def _fence_snippet(snippet: str) -> str:
    """코드 스니펫의 모든 줄 앞에 표식을 붙인다.

    스니펫은 고객 레포의 소스라 신뢰할 수 없는데, 줄바꿈을 살려야 코드로 읽히므로
    _clean()처럼 한 줄로 접을 수 없다. 그래서 내용을 건드리는 대신 경계를 못 넘게 만든다.

    표식은 서버가 무조건 붙이므로 어떤 줄도 "- id:"로 시작할 수 없다. 근거 목록이
    이 블록보다 앞에 있으니 뒤에서 항목을 덧붙일 수도 없다.
    줄 내용을 지우거나 고치지 않는다 — 타입 어노테이션(`kind: str`) 같은 정상 코드를
    훼손하면 "근거가 이 함수 이야기가 맞는지" 확인하는 본래 목적이 사라진다.
    """
    return "\n".join(f"│ {line}" for line in snippet.splitlines())


def _format_date(value: datetime | None) -> str:
    # 없는 날짜를 "미상"으로 넘기면 프롬프트 규칙 5가 연월을 창작하지 않게 막는다.
    return value.strftime("%Y-%m") if value else "미상"


# 날짜 없는 근거(미병합 PR)를 정렬할 때 쓰는 기준값. 가장 오래된 것으로 취급한다.
# 튜플 정렬에 reverse=True를 걸면 None 항목이 오히려 맨 앞으로 오는 함정이 있어 센티널을 쓴다.
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime | None) -> datetime:
    """정렬용 비교 기준. naive/aware가 섞여도 터지지 않게 맞춘다.

    Postgres는 aware를 돌려주지만 SQLite는 naive를 돌려준다. 섞이면 비교에서 TypeError가 난다.
    """
    if value is None:
        return _OLDEST
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_noise(item: EvidenceItem) -> bool:
    """LLM에 보내기도 아까운 확실한 잡음인지.

    본문이 없고 chore/style로 시작하며 제목에 숫자도 화살표도 없는 것만 거른다.
    "chore: 토큰 만료 1시간->15분"처럼 실제 동작이 바뀐 건 숫자·화살표 때문에 살아남는다.
    머지 커밋은 제목에 PR 번호가 있어 숫자 규칙으로는 안 걸리므로 따로 본다.
    확실한 것만 여기서 걸러내고, 나머지 판단은 LLM 1단계에 맡긴다(이중 방어).
    """
    if _MERGE_COMMIT.match(item.title.strip()):
        return True
    if item.body:
        return False
    title = item.title.strip().lower()
    if not title.startswith(("chore:", "style:")):
        return False
    return not re.search(r"[\d]|->|→", item.title)


def load_evidence(db: Session, repo: Repo, symbol_idents: list[str]) -> list[EvidenceItem]:
    """심볼들의 근거를 시간 오름차순으로 모은다.

    프롬프트가 시간순 서사를 요구하므로 입력 순서도 시간순으로 맞춘다.
    번호 순서와 이야기 순서가 어긋나면 소형 모델이 앞뒤를 뒤집는다.

    커밋·PR을 id로 하나씩 꺼내지 않고 한 번에 읽는다. 근거가 수십 건이면 왕복이 그만큼 늘고,
    무엇보다 db.get()은 조직 필터를 타지 않아 격리 규칙(src/db/query.py)의 방어선이 사라진다.
    """
    if not symbol_idents:
        return []

    rows = list(
        db.scalars(
            query(SymbolEvidence, repo.organization_id)
            .where(SymbolEvidence.repo_id == repo.id)
            .where(SymbolEvidence.symbol_ident.in_(symbol_idents))
        )
    )

    commit_ids = {r.commit_id for r in rows if r.kind == "commit" and r.commit_id}
    pr_ids = {r.pull_request_id for r in rows if r.kind == "pr" and r.pull_request_id}

    commits = (
        {c.id: c for c in db.scalars(query(Commit, repo.organization_id).where(Commit.id.in_(commit_ids)))}
        if commit_ids
        else {}
    )
    prs = (
        {p.id: p for p in db.scalars(query(PullRequest, repo.organization_id).where(PullRequest.id.in_(pr_ids)))}
        if pr_ids
        else {}
    )

    items: list[EvidenceItem] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if row.kind == "commit" and row.commit_id in commits:
            commit = commits[row.commit_id]
            key = ("commit", commit.id)
            if key in seen:
                continue  # 같은 커밋이 여러 심볼의 근거일 수 있다 (파일 단위 맥락)
            seen.add(key)
            items.append(
                EvidenceItem(
                    id="",
                    kind="commit",
                    title=commit.title,
                    body=commit.body,
                    occurred_at=commit.committed_at,
                    source=commit,
                )
            )
        elif row.kind == "pr" and row.pull_request_id in prs:
            pr = prs[row.pull_request_id]
            key = ("pr", pr.id)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                EvidenceItem(
                    id="",
                    kind="pr",
                    title=pr.title,
                    body=pr.body,
                    occurred_at=pr.merged_at,
                    number=pr.number,
                    review=pr.review_excerpt,
                    source=pr,
                )
            )

    items.sort(key=lambda i: _as_utc(i.occurred_at))
    return items


def select_for_summary(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """요약에 실제로 쓸 근거. 잡음을 뺀 최근 MAX_EVIDENCE건 (시간 오름차순)."""
    return [item for item in items if not _is_noise(item)][-MAX_EVIDENCE:]


def _worth_summarizing(usable: list[EvidenceItem]) -> bool:
    """LLM을 부를 값어치가 있는 근거인지.

    근거가 하나뿐이고 본문도 없으면 "왜 그렇게 작성됐는지"에 답할 재료가 없다.
    제목만으로 요약을 만들면 "초기 구조에서 추가된 함수" 같은 문장이 나오는데,
    이건 명세 §4가 금지한 추측 서술에 가깝고 no_history가 정직한 답이다.

    실측(데모 레포 심볼 114개): 근거 1건인 심볼 98개 중 95개가 본문 없는 파일 생성 커밋이다.
    이 판정으로 LLM 호출이 114회에서 19회로 줄어 분당 10회 제한 안에 들어온다.
    """
    if not usable:
        return False
    if len(usable) == 1 and not usable[0].body:
        return False
    return True


def select_for_display(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """화면에 보여줄 근거. 최신순.

    요약이 인용한 근거를 반드시 포함해야 한다. 요약은 "근거에 적힌 것만 썼다"고 주장하는데
    그 근거가 목록에 없으면 사용자가 확인할 방법이 없고, 이 제품의 핵심 주장이 화면에서 깨진다.
    그래서 요약이 쓴 것을 먼저 채우고, 남는 자리에만 걸러낸 잡음을 최신순으로 넣는다.
    """
    used = select_for_summary(items)
    used_ids = {id(item) for item in used}
    display = list(used)
    for item in reversed(items):
        if len(display) >= MAX_EVIDENCE:
            break
        if id(item) not in used_ids:
            display.append(item)
    # 무엇을 담을지 정한 뒤에 정렬한다. 채우면서 순서를 만들면 요약이 쓴 오래된 근거가
    # 나중에 채운 최신 잡음보다 앞에 오는 뒤죽박죽 목록이 된다.
    display.sort(key=lambda i: _as_utc(i.occurred_at), reverse=True)
    return display


def render_prompt(symbol: Symbol, snippet: str, items: list[EvidenceItem]) -> tuple[str, list[EvidenceItem]]:
    """프롬프트 본문과 실제로 실린 근거 목록을 함께 돌려준다."""
    blocks = []
    for item in items:
        # 제목의 (#N)은 여기서 지운다. 모델이 볼 수 없으면 인용할 수도 없다.
        title = _clean(_TRAILING_PR_TAG.sub("", item.title), 200)
        body = _clean(item.body, 400)
        if item.kind == "pr":
            blocks.append(
                PR_ITEM.format(
                    id=item.id,
                    number=item.number,
                    date=_format_date(item.occurred_at),
                    title=title,
                    body=body,
                    review=_clean(item.review, 400),
                )
            )
        else:
            blocks.append(
                COMMIT_ITEM.format(id=item.id, date=_format_date(item.occurred_at), title=title, body=body)
            )

    user = USER_TEMPLATE.format(
        # 심볼 이름과 경로도 파서가 소스에서 뽑아온 값이라 신뢰할 수 없다.
        symbol_ident=_clean(symbol.ident, 600),
        path=_clean(symbol.path, 500),
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        snippet=_fence_snippet(snippet) if snippet else "│ (코드 없음)",
        evidence_count=len(items),
        evidence_block="\n".join(blocks),
    )
    return user, items


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _sentence_is_grounded(
    sentence: dict,
    valid_ids: set[str],
    dropped: set[str],
    allowed_pr_numbers: set[int],
    allowed_years: set[str],
    allowed_months: set[str],
) -> tuple[bool, str]:
    """문장 하나가 근거를 제대로 대고 있는지. 실패 이유도 함께 돌려준다."""
    text = sentence.get("text")
    cited = sentence.get("evidence")
    # 모델이 타입을 어기면(text가 숫자, evidence가 dict) 아래에서 터진다.
    # 예외로 죽으면 재시도 없이 실패 카운터만 오르므로 여기서 걸러 정상 재시도 경로로 보낸다.
    if not isinstance(text, str) or not isinstance(cited, list):
        return False, "출력 형식이 잘못됨"
    text = text.strip()

    if not text:
        return False, "빈 문장"
    if len(text) > MAX_SENTENCE_LENGTH:
        return False, "문장이 너무 김"
    if text.lstrip().startswith(_MARKDOWN_START):
        return False, "마크다운 기호"
    # 인용 없는 문장은 코드 스니펫이나 상상에서 나온 것이다. 코드 블록에는 id가 없으므로
    # 코드에서 유도한 문장은 구조적으로 유효한 인용을 만들 수 없다.
    if not cited or not set(cited).issubset(valid_ids):
        return False, "근거 인용 없음 또는 없는 id 인용"
    if set(cited) & dropped:
        return False, "버린 근거를 인용"
    found = _SPECULATION.search(text)
    if found:
        return False, f"추측 표현: {found.group()}"
    # 근거에 없는 PR 번호를 지어냈는지.
    for number in re.findall(r"#(\d+)", text):
        if int(number) not in allowed_pr_numbers:
            return False, f"근거에 없는 번호 #{number}"
    # 근거에 없는 연도를 지어냈는지.
    for year in re.findall(r"(\d{4})년", text):
        if year not in allowed_years:
            return False, f"근거에 없는 연도 {year}"
    # 연월까지 쓴 문장은 월도 맞아야 한다. 연도만 보면 "2023년 3월"을 "2023년 11월"로
    # 바꿔 써도 통과한다 — 명세 §4의 요약 예시가 월을 쓰므로 실제로 나올 수 있는 오류다.
    for year, month in re.findall(r"(\d{4})\s*년\s*(\d{1,2})\s*월", text):
        if allowed_months and f"{year}-{int(month):02d}" not in allowed_months:
            return False, f"근거에 없는 시점 {year}년 {month}월"
    return True, ""


@dataclass
class SummaryResult:
    status: str  # ok | no_history | conflicting
    summary: str | None


def _validate(payload: dict, items: list[EvidenceItem]) -> SummaryResult | None:
    """모델 출력을 서버 기준으로 재판정한다. 쓸 수 없는 출력이면 None을 돌려준다.

    None(검증 실패)과 no_history(이력 없음)를 반드시 구분한다. 문장을 냈는데 근거를 못 대서
    전부 버려진 것은 모델이 실패한 것이지 "이 함수에 이력이 없다"가 아니다.
    이걸 섞으면 근거가 멀쩡한 함수가 "이력 없음"으로 캐시되어 영영 그렇게 보인다.
    """
    if not isinstance(payload, dict):
        return None

    valid_ids = {item.id for item in items}
    dropped = {i for i in (payload.get("dropped") or []) if isinstance(i, str)}

    # 근거에 적힌 값도 인용할 수 있어야 한다. 프롬프트가 "본문에 이유가 있으면 그 표현을
    # 살려 쓰라"고 요구하는데, 본문의 "2019년 장애 이후"나 "관련 이슈 #45"를 충실히
    # 인용한 문장이 여기서 버려지면 가장 값진 서술이 사라진다.
    # 단 모델이 실제로 본 텍스트(_clean으로 잘린 것)만 허용한다. 잘려나간 뒤쪽 내용을
    # 근거로 인정하면 모델이 보지도 않은 값을 통과시키게 된다.
    allowed_years = {item.occurred_at.strftime("%Y") for item in items if item.occurred_at is not None}
    allowed_months = {item.occurred_at.strftime("%Y-%m") for item in items if item.occurred_at is not None}
    allowed_pr_numbers = {item.number for item in items if item.kind == "pr" and item.number}
    for item in items:
        seen_text = " ".join(
            (_clean(item.title, 200), _clean(item.body, 400), _clean(item.review, 400))
        )
        # "3000ms" 같은 값이 연도로 통과하지 않게 "년"이 붙은 것만 인정한다.
        allowed_years.update(re.findall(r"(\d{4})\s*년", seen_text))
        allowed_pr_numbers.update(int(n) for n in re.findall(r"#(\d+)", seen_text))

    sentences = payload.get("sentences") or []
    kept: list[str] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        ok, reason = _sentence_is_grounded(
            sentence, valid_ids, dropped, allowed_pr_numbers, allowed_years, allowed_months
        )
        if ok:
            kept.append(sentence["text"].strip())
        else:
            # 폐기 사유를 남길 때도 모델 출력을 신뢰하지 않는다. text가 문자열이 아닐 수 있다.
            logger.info("요약 문장 폐기 (%s): %s", reason, str(sentence.get("text"))[:60])

    if not kept:
        if not sentences and payload.get("status") == "no_history":
            # 모델이 "쓸 만한 이력이 없다"고 판단했고 문장도 내지 않았다. 정당한 판정이다.
            return SummaryResult(status="no_history", summary=None)
        # 문장을 냈는데 전부 근거를 못 댔다(또는 문장 없이 ok라고 했다). 모델 실패다.
        return None

    status = payload.get("status")
    if status not in ("ok", "no_history", "conflicting"):
        status = "ok"
    if status == "no_history":
        # 문장이 남았는데 no_history라고 한 건 모순이다. 문장을 살린다.
        status = "ok"

    if status == "conflicting":
        sides = payload.get("conflict_sides")
        valid_sides = (
            isinstance(sides, list)
            and len(sides) == 2
            and all(isinstance(s, list) and s and set(s).issubset(valid_ids) for s in sides)
            # 같은 근거를 양쪽에 적어놓은 건 상충이 아니다. 서로 다른 근거여야 "양쪽"이다.
            and not (set(sides[0]) & set(sides[1]))
        )
        if not valid_sides:
            # 상충이라면서 양쪽을 못 대면 상충이 아니다. 기본값으로 강등한다.
            logger.info("conflicting 판정을 ok로 강등: conflict_sides 불충분")
            status = "ok"

    summary = " ".join(kept)
    while len(summary) > MAX_SUMMARY_LENGTH and len(kept) > 1:
        kept.pop(0)
        summary = " ".join(kept)
    return SummaryResult(status=status, summary=summary[:MAX_SUMMARY_LENGTH])


def summarize(symbol: Symbol, snippet: str, items: list[EvidenceItem]) -> SummaryResult | None:
    """근거로 요약을 만든다. LLM 호출과 검증까지 담당한다.

    근거가 없으면 LLM을 부르지 않는다 (완료 조건 2번을 모델과 무관하게 보장하고 비용도 아낀다).
    재시도까지 실패하면 None을 돌려준다 — 호출자는 이걸 저장하지 않아야 다음에 다시 시도된다.
    """
    usable = select_for_summary(items)
    if not _worth_summarizing(usable):
        return SummaryResult(status="no_history", summary=None)

    numbered = [
        EvidenceItem(
            id=f"e{index}",
            kind=item.kind,
            title=item.title,
            body=item.body,
            occurred_at=item.occurred_at,
            number=item.number,
            review=item.review,
            source=item.source,
        )
        for index, item in enumerate(usable, start=1)
    ]

    user, numbered = render_prompt(symbol, snippet, numbered)

    for attempt, extra in enumerate(("", RETRY_SUFFIX.format(reason="근거 id를 정확히 인용하고 JSON만 출력하라")), start=1):
        try:
            raw = complete(SYSTEM_PROMPT, user + extra)
            payload = _parse_json(raw)
        except LLMUnavailable:
            raise
        except (json.JSONDecodeError, ValueError) as error:
            logger.warning("요약 응답 파싱 실패 (%d회차): %s", attempt, error)
            continue

        result = _validate(payload, numbered)
        if result is not None:
            return result
        logger.warning("요약 검증 실패 (%d회차): %s", attempt, symbol.ident)

    logger.warning("요약 생성 실패: %s", symbol.ident)
    return None


def generate_summary(db: Session, repo: Repo, symbol: Symbol, snippet: str = "") -> SymbolSummary | None:
    """심볼 하나의 요약을 만들어 저장한다. 실패하면 저장하지 않고 None을 돌려준다.

    실패한 요약을 저장하지 않는 이유: 잘못된 캐시가 남으면 다음 조회에서도 계속 비어 보인다.
    """
    items = load_evidence(db, repo, [symbol.ident])
    try:
        result = summarize(symbol, snippet, items)
    except LLMUnavailable as error:
        # 키가 없거나 OpenAI가 죽어도 인덱싱 전체를 실패시키지 않는다.
        logger.warning("요약 생략 (%s): %s", symbol.ident, error)
        return None

    if result is None:
        # 저장하지 않는다. 근거가 있는 심볼을 "이력 없음"이나 빈 요약으로 굳히면
        # 캐시가 남아 다시 시도되지 않고, 화면에는 영영 그 상태로 보인다.
        return None

    row = db.scalar(
        query(SymbolSummary, repo.organization_id)
        .where(SymbolSummary.repo_id == repo.id)
        .where(SymbolSummary.symbol_ident == symbol.ident)
    )
    if row is None:
        row = SymbolSummary(
            organization_id=repo.organization_id, repo_id=repo.id, symbol_ident=symbol.ident
        )
        db.add(row)
    row.status = result.status
    row.summary = result.summary
    row.generated_at = datetime.now(timezone.utc)
    db.commit()
    return row
