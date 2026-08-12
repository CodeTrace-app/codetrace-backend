"""레포를 파싱해 파일·심볼·참조를 저장한다 (이슈 #14).

흐름:
    레포 디렉터리 훑기 → 파일 저장 → 심볼 추출 → 참조 대상 확정 → 참조 수 집계

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
    """재인덱싱은 전체를 다시 만든다. 지우지 않으면 사라진 함수가 그래프에 남는다."""
    for model in (Reference, Symbol, SourceFile):
        for row in db.scalars(query(model, repo.organization_id).where(model.repo_id == repo.id)):
            db.delete(row)
    db.flush()


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
    """레포를 파싱해 저장하고 (심볼 수, 참조 수)를 돌려준다."""
    _clear_previous(db, repo)

    files = list(_iter_files(repo_dir))
    repo.progress_current = 0
    repo.progress_total = len(files)
    db.commit()

    all_symbols: list[ParsedSymbol] = []
    all_references: list[ParsedReference] = []
    stored_files = 0

    for index, path in enumerate(files, start=1):
        relative = path.relative_to(repo_dir).as_posix()
        content = _read(path)
        if content is None:
            continue

        # 파서가 없는 파일도 저장한다. 파일 트리와 코드 뷰어는 README도 보여준다.
        db.add(
            SourceFile(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                path=relative,
                language=language_of(relative),
                content=content,
            )
        )
        stored_files += 1

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
            repo.progress_current = index
            db.commit()

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

    repo.files_count = stored_files
    repo.functions_count = len(all_symbols)
    repo.progress_current = len(files)
    db.commit()

    logger.info(
        "파싱 완료 repo_id=%s 파일=%d 심볼=%d 참조=%d 건너뜀=%d",
        repo.id, stored_files, len(all_symbols), len(resolved), skipped,
    )
    return len(all_symbols), len(resolved)
