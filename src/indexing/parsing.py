"""레포를 파싱해 파일·심볼·참조를 저장한다 (이슈 #14).

흐름:
    레포 디렉터리 훑기 → 심볼 추출 → (여기까지 저장 없음)
    → 이전 인덱스 삭제 → 파일·심볼 저장 → 참조 대상 확정 → 참조 수 집계 → 커밋

확실하지 않은 참조는 저장하지 않는다. 커버리지보다 정확도가 우선이다.
없는 관계를 보여주지 않는 것이 이 제품의 주장이므로, 대상을 특정할 수 없으면 버린다.
"""

import json
import logging
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Reference, Repo, SourceFile, Symbol
from src.db.query import query
from src.parser import ParsedReference, ParsedSymbol, get_adapter, language_of

logger = logging.getLogger(__name__)

# 인덱싱에서 건너뛸 디렉터리. 남의 코드까지 넣으면 그래프가 의미를 잃고 용량만 커진다.
SKIP_DIRS = {
    ".git", ".github", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "out", "coverage", ".pytest_cache", ".mypy_cache",
}

# 코드 뷰어가 감당할 크기. 넘으면 잘라서 저장하고 화면에서 안내한다 (api-spec §4).
MAX_FILE_BYTES = 400_000

# 트리에 넣을 파일 확장자. 지원 언어가 아니어도 읽을 수 있는 것은 보여준다.
READABLE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sql", ".sh",
}


def _iter_files(repo_dir: Path):
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(repo_dir).parts):
            continue
        if path.suffix.lower() not in READABLE_SUFFIXES:
            continue
        yield path


def _read(path: Path) -> str | None:
    """파일을 읽는다. 바이너리이거나 너무 크면 None.

    인덱싱은 레포 전체를 훑으므로 파일 하나 때문에 멈추면 안 된다.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _clear_previous(db: Session, repo: Repo) -> None:
    """재인덱싱은 전체를 다시 만든다. 지우지 않으면 사라진 함수가 그래프에 남는다.

    호출 시점이 중요하다. 파싱을 시작하기 전에 지우고 커밋하면, 그 뒤 어디서든
    실패했을 때 이전 인덱스까지 사라진다. 새 데이터를 다 만든 뒤 저장 직전에
    지우고, 저장이 끝날 때까지 커밋하지 않는다. 실패하면 rollback으로 이전 인덱스가 남는다.
    """
    for model in (Reference, Symbol, SourceFile):
        for row in db.scalars(query(model, repo.organization_id).where(model.repo_id == repo.id)):
            db.delete(row)
    # 여기서 flush해 DELETE를 먼저 내보낸다. 새 행을 add한 뒤에 flush하면 SQLAlchemy가
    # INSERT를 먼저 실행해 (repo_id, path)·(repo_id, ident) 유니크 제약에 걸린다.
    db.flush()


def _drop_duplicate_idents(symbols: list[ParsedSymbol]) -> list[ParsedSymbol]:
    """ident가 겹치는 심볼을 하나만 남긴다.

    Symbol에 UniqueConstraint(repo_id, ident)가 있어서, 겹친 채로 저장하면
    IntegrityError로 인덱싱 전체가 실패한다. 파서가 이름을 최대한 구분하지만
    @overload처럼 같은 이름을 의도적으로 여러 번 정의하는 문법이 남는다.

    나중 정의를 남긴다. 파이썬은 마지막 정의가 실제로 동작하는 쪽이고,
    @overload도 마지막이 구현부다.
    """
    by_ident: dict[str, ParsedSymbol] = {}
    for symbol in symbols:
        by_ident[symbol.ident] = symbol

    dropped = len(symbols) - len(by_ident)
    if dropped:
        logger.info("이름이 겹치는 정의 %d개를 마지막 것만 남기고 접었습니다", dropped)
    return list(by_ident.values())


def _resolve_targets(
    references: list[ParsedReference], symbols_by_name: dict[str, list[str]]
) -> tuple[list[tuple[ParsedReference, str]], int]:
    """참조가 가리키는 대상을 확정한다.

    파일 하나만 봐서는 process_payment()가 어느 파일의 함수인지 알 수 없다.
    레포 전체 심볼과 대조해 후보가 하나뿐일 때만 연결한다.

    후보가 여럿이면 저장하지 않는다. import를 추출하는 다음 이슈(#20)에서
    범위를 좁히면 이 중 일부가 살아난다. 틀린 관계를 보여주느니 비워둔다.
    """
    resolved: list[tuple[ParsedReference, str]] = []
    skipped = 0

    for ref in references:
        candidates = symbols_by_name.get(ref.target_name, [])
        if len(candidates) == 1:
            resolved.append((ref, candidates[0]))
        else:
            # 후보 0개는 외부 라이브러리 호출(httpx.post 등), 2개 이상은 동명이인이다.
            skipped += 1

    return resolved, skipped


def _update_reference_counts(db: Session, repo: Repo) -> None:
    """심볼별 피참조 횟수를 채운다.

    그래프가 15개를 넘으면 이 값으로 정렬해 자른다 (api-spec §4).
    """
    counts = dict(
        db.execute(
            select(Reference.target_ident, func.count())
            .where(Reference.repo_id == repo.id, Reference.organization_id == repo.organization_id)
            .group_by(Reference.target_ident)
        ).all()
    )
    for symbol in db.scalars(query(Symbol, repo.organization_id).where(Symbol.repo_id == repo.id)):
        symbol.reference_count = counts.get(symbol.ident, 0)


def parse_repo(db: Session, repo: Repo, repo_dir: Path) -> tuple[int, int]:
    """레포를 파싱해 저장하고 (심볼 수, 참조 수)를 돌려준다.

    읽기·파싱을 먼저 다 끝내고, 저장은 마지막에 한 트랜잭션으로 한다.
    중간에 실패해도 이전 인덱스가 그대로 남아 화면이 비지 않는다.
    """
    files = list(_iter_files(repo_dir))
    repo.progress_current = 0
    repo.progress_total = len(files)
    db.commit()

    all_symbols: list[ParsedSymbol] = []
    all_references: list[ParsedReference] = []
    # 파서가 없는 파일도 담는다. 파일 트리와 코드 뷰어는 README도 보여준다.
    read_files: list[tuple[str, str | None, str]] = []

    for index, path in enumerate(files, start=1):
        relative = path.relative_to(repo_dir).as_posix()
        content = _read(path)
        if content is None:
            continue

        read_files.append((relative, language_of(relative), content))

        adapter = get_adapter(relative)
        if adapter is not None:
            try:
                symbols, references = adapter.parse(relative, content)
            except Exception:
                # 파일 하나가 파서를 넘어뜨려도 인덱싱 전체가 실패하면 안 된다.
                logger.exception("파싱 실패, 건너뜁니다: %s", relative)
            else:
                all_symbols.extend(symbols)
                all_references.extend(references)

        if index % 50 == 0:
            # 진행률만 커밋한다. 새 인덱스는 아직 세션에 넣지 않았으므로
            # 여기서 커밋해도 이전 인덱스는 그대로다.
            repo.progress_current = index
            db.commit()

    all_symbols = _drop_duplicate_idents(all_symbols)

    # ---- 여기서부터 저장. 이전 인덱스를 지우고 새 것을 넣을 때까지 커밋하지 않는다.
    _clear_previous(db, repo)

    for relative, language, content in read_files:
        db.add(
            SourceFile(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                path=relative,
                language=language,
                content=content,
            )
        )

    for symbol in all_symbols:
        db.add(
            Symbol(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                ident=symbol.ident,
                name=symbol.name,
                path=symbol.path,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                params=json.dumps(symbol.params, ensure_ascii=False),
            )
        )
    db.flush()

    symbols_by_name: dict[str, list[str]] = defaultdict(list)
    for symbol in all_symbols:
        symbols_by_name[symbol.name].append(symbol.ident)
        # obj.method() 호출은 클래스명 없이 메서드 이름만 보이므로 그 이름으로도 찾을 수 있게 한다.
        if "." in symbol.name:
            symbols_by_name[symbol.name.rsplit(".", 1)[-1]].append(symbol.ident)

    resolved, skipped = _resolve_targets(all_references, symbols_by_name)
    for ref, target_ident in resolved:
        db.add(
            Reference(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                source_ident=ref.source_ident,
                target_ident=target_ident,
                ref_type=ref.ref_type,
                path=ref.path,
                line=ref.line,
            )
        )
    db.flush()

    _update_reference_counts(db, repo)

    repo.files_count = len(read_files)
    repo.functions_count = len(all_symbols)
    repo.progress_current = len(files)
    db.commit()

    logger.info(
        "파싱 완료 repo_id=%s 파일=%d 심볼=%d 참조=%d 건너뜀=%d",
        repo.id, len(read_files), len(all_symbols), len(resolved), skipped,
    )
    return len(all_symbols), len(resolved)
