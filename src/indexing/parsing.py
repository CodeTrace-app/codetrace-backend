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
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Reference, Repo, SourceFile, Symbol
from src.db.query import query
from src.parser import ParsedImport, ParsedReference, ParsedSymbol, get_adapter, language_of

logger = logging.getLogger(__name__)

# 인덱싱에서 건너뛸 디렉터리. 남의 코드까지 넣으면 그래프가 의미를 잃고 용량만 커진다.
SKIP_DIRS = {
    ".git", ".github", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "out", "coverage", ".pytest_cache", ".mypy_cache",
}

# 코드 뷰어가 감당할 크기. 넘으면 잘라서 저장하고 화면에서 안내한다 (api-spec §4).
MAX_FILE_BYTES = 400_000

# 심볼이 아니라 파일(모듈) 자체를 가리킬 때 ident 뒤에 붙이는 표시.
# import는 모듈 레벨에서 일어나 출발 심볼이 없기 때문에 필요하다.
# 꺾쇠는 파이썬 식별자에 쓸 수 없어 실제 심볼 이름과 겹치지 않는다.
# 그래프 API(#23)는 이 표시가 붙은 ident를 노드로 만들지 않는다.
MODULE_IDENT = "::<module>"

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
    """파일을 읽는다. 바이너리면 None.

    큰 파일은 상한까지만 읽어 저장한다. 통째로 버리면 파일 트리에서 사라져
    "일부만 표시됨" 안내조차 띄울 수 없다 (api-spec §4의 truncated).
    잘렸다는 사실은 저장된 크기가 상한에 닿았는지로 판별한다.

    인덱싱은 레포 전체를 훑으므로 파일 하나 때문에 멈추면 안 된다.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None

    cut = len(raw) > MAX_FILE_BYTES
    raw = raw[:MAX_FILE_BYTES]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        # 마지막 글자가 잘려서 난 오류면 그 앞까지가 온전한 텍스트다.
        # 그보다 앞에서 났다면 바이너리 파일이므로 저장하지 않는다.
        if cut and error.start >= len(raw) - 4:
            return raw[: error.start].decode("utf-8")
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


# TS/JS의 import는 확장자를 생략한다. './client'가 어느 파일인지 이 순서로 찾는다.
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


def _module_paths(file_paths: list[str]) -> dict[str, str]:
    """모듈 식별자 → 파일 경로. 레포 안에 실제로 있는 파일만 들어간다.

    파이썬은 점 표기(src.payment), TS/JS는 확장자 없는 경로(src/api/client)를 쓴다.
    어느 쪽이든 여기 없는 것이 외부 라이브러리다. httpx나 react는 레포에 파일이
    없으므로 자연히 걸러진다 (이슈 #20 완료 조건).
    """
    modules: dict[str, str] = {}
    for path in file_paths:
        if path.endswith(".py"):
            if path == "__init__.py":
                continue
            if path.endswith("/__init__.py"):
                dotted = path[: -len("/__init__.py")].replace("/", ".")
            else:
                dotted = path[: -len(".py")].replace("/", ".")
            if dotted:
                modules[dotted] = path
            continue

        if path.endswith(_TS_SUFFIXES):
            without_suffix = path.rsplit(".", 1)[0]
            # 먼저 등록된 것을 남긴다. client.ts와 client/index.ts가 함께 있으면
            # 확장자만 뗀 경로가 가리키는 client.ts가 우선이다.
            modules.setdefault(without_suffix, path)
            # './api'로 폴더를 가리키면 그 안의 index 파일이 대상이다.
            if without_suffix.endswith("/index"):
                modules.setdefault(without_suffix[: -len("/index")], path)
    return modules


@dataclass(frozen=True)
class ImportIndex:
    """import에서 얻은, 이름이 어느 파일에서 왔는지에 대한 지식 (이슈 #20).

    둘 다 "파일 경로 → {그 파일에서 쓰는 이름: 그 이름이 사는 파일}" 형태다.
    레포 밖 모듈은 어느 쪽에도 들어오지 않는다. 외부 라이브러리가 걸러지는 지점이다.
    """

    # from src.payment import process_payment → {"process_payment": "src/payment.py"}
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    # import src.payment as pay → {"pay": "src/payment.py"}
    modules: dict[str, dict[str, str]] = field(default_factory=dict)
    # 레포 밖에서 들여온 이름. {"src/page.tsx": {"useState", "axios"}}
    # 이 이름들은 레포 안에 같은 이름이 있어도 그것을 가리키지 않는다.
    external: dict[str, set[str]] = field(default_factory=dict)


def _build_import_index(
    imports_by_file: dict[str, list[ParsedImport]], modules: dict[str, str]
) -> ImportIndex:
    """파일별로 "이 이름은 저 파일에서 왔다"를 만든다.

    `from src.payment import process_payment`을 읽어두면 그 파일 안의
    process_payment() 호출이 어느 파일 것인지 확정된다. 이름이 겹쳐서
    버려지던 호출들이 여기서 살아난다.

    `import src.github.git_history as git_history` 쪽은 이름이 모듈을 가리킨다.
    호출부에 git_history.list_commits()로 나타나므로 수신자와 맞춰 확정한다.
    """
    index = ImportIndex()
    for path, items in imports_by_file.items():
        for item in items:
            if not item.local_name:
                # import './setup' 처럼 이름을 들여오지 않는 import. 의존 근거로만 남는다.
                continue

            if item.module not in modules and f"{item.module}.{item.origin_name}" not in modules:
                # 레포 밖에서 온 이름이다. react의 useState는 레포 안에 같은 이름의
                # 함수가 있어도 그것이 아니다. 여기 적어두지 않으면 후보가 하나뿐일 때
                # 그 이름으로 이어져 없는 관계가 그려진다.
                index.external.setdefault(path, set()).add(item.local_name)
                continue

            if item.origin_name is None:
                # `import src.payment` — 이름이 모듈을 가리킨다.
                target = modules.get(item.module)
                if target is not None:
                    index.modules.setdefault(path, {})[item.local_name] = target
                continue

            # `from src import payment`도 모듈을 들여오는 형태다.
            submodule = modules.get(f"{item.module}.{item.origin_name}")
            if submodule is not None:
                index.modules.setdefault(path, {})[item.local_name] = submodule

            target = modules.get(item.module)
            if target is not None:
                index.names.setdefault(path, {})[item.local_name] = target
    return index


def _is_external(ref: ParsedReference, index: ImportIndex) -> bool:
    """레포 밖에서 들여온 이름을 쓴 참조인가.

    react의 useState()나 axios.get()은 레포 안에 같은 이름이 있어도 그것이 아니다.
    후보가 하나뿐이면 확정해버리는 규칙보다 먼저 봐야 한다.
    """
    external = index.external.get(ref.path)
    if not external:
        return False
    return (ref.receiver in external) if ref.receiver is not None else (ref.target_name in external)


def _narrow(candidates: list[str], ref: ParsedReference, index: ImportIndex) -> list[str]:
    """후보가 여럿일 때 범위를 좁힌다. 근거가 강한 순서대로 본다.

    1. `git_history.list_commits()`처럼 수신자가 import한 모듈이면 그 파일로 확정된다.
       파이썬이 이 호출을 해석하는 방식 그대로이므로 다른 규칙보다 확실하다.
       그 파일에 그 이름이 없으면 다른 후보로 넘어가지 않고 버린다.
    2. import로 이름이 묶여 있으면 그 파일 것만 남긴다.
    3. 그래도 여럿이면 같은 파일 안의 정의를 고른다. 이름이 겹쳐도 파이썬은
       import하지 않은 이상 자기 파일 것을 부른다. 단 `obj.f()`처럼 다른 객체를
       거친 호출에는 쓰지 않는다. 그 f는 이 파일의 f가 아니라 obj의 것이다.
    """
    if ref.receiver is not None:
        module_path = index.modules.get(ref.path, {}).get(ref.receiver)
        if module_path is not None:
            return [c for c in candidates if c.split("::", 1)[0] == module_path]

    bound_path = index.names.get(ref.path, {}).get(ref.target_name)
    if bound_path is not None:
        bound = [c for c in candidates if c.split("::", 1)[0] == bound_path]
        if bound:
            return bound

    if ref.receiver is not None and ref.receiver not in ("self", "cls"):
        return candidates

    same_file = [c for c in candidates if c.split("::", 1)[0] == ref.path]
    return same_file or candidates


def _resolve_targets(
    references: list[ParsedReference],
    symbols_by_name: dict[str, list[str]],
    index: ImportIndex | None = None,
) -> tuple[list[tuple[ParsedReference, str]], int]:
    """참조가 가리키는 대상을 확정한다.

    파일 하나만 봐서는 process_payment()가 어느 파일의 함수인지 알 수 없다.
    레포 전체 심볼과 대조하고, 후보가 여럿이면 import로 범위를 좁힌다(#20).

    좁히고도 여럿이면 저장하지 않는다. 틀린 관계를 보여주느니 비워둔다.
    """
    index = index or ImportIndex()
    resolved: list[tuple[ParsedReference, str]] = []
    skipped = 0

    for ref in references:
        if _is_external(ref, index):
            skipped += 1
            continue

        candidates = symbols_by_name.get(ref.target_name, [])
        if len(candidates) > 1:
            candidates = _narrow(candidates, ref, index)

        if len(candidates) == 1:
            resolved.append((ref, candidates[0]))
        else:
            # 후보 0개는 외부 라이브러리 호출(httpx.post 등), 2개 이상은 동명이인이다.
            skipped += 1

    return resolved, skipped


def _import_references(
    imports_by_file: dict[str, list[ParsedImport]], modules: dict[str, str]
) -> list[tuple[str, ParsedImport, str, str]]:
    """import 관계를 근거로 남긴다. (파일 경로, import, 출발 ident, 도착 ident) 목록.

    PRD가 추적하라고 한 네 가지 관계 중 하나라 버리지 않는다. "이 파일이 왜
    영향받는다고 판단했는가"의 답이 여기 있고, 나중에 import 기반 영향 분석을
    붙일 때 파서를 다시 뜯지 않아도 된다.

    다만 그래프 노드로 만들지는 않는다. import는 파일 맨 위, 모듈 레벨에서
    일어나 출발 심볼이 없고, api-spec §4의 노드 kind는 function|constant|class
    뿐이다. 외부 라이브러리까지 노드로 만들면 프로젝트 밖을 인덱싱하게 된다.
    그래서 출발점은 파일을 가리키는 표시(MODULE_IDENT)로 두고, 그래프에 올릴
    것은 #23에서 실제 심볼로 해석되는 것만 고른다.
    """
    rows: list[tuple[str, ParsedImport, str, str]] = []
    for path, items in imports_by_file.items():
        source = f"{path}{MODULE_IDENT}"
        for item in items:
            target_file = modules.get(item.module)

            if item.origin_name is None:
                # `import x` — 모듈 자체를 들여온다.
                target = f"{target_file}{MODULE_IDENT}" if target_file else item.module
            elif (submodule := modules.get(f"{item.module}.{item.origin_name}")) is not None:
                # `from src import payment` — 들여온 것이 하위 모듈이다.
                # 패키지에 __init__.py가 없어 상위가 모듈로 안 잡히는 경우도 여기서 걸린다.
                target = f"{submodule}{MODULE_IDENT}"
            elif target_file is not None:
                # 레포 안의 이름이다. 아직 심볼로 추출되지 않은 상수여도
                # ident 형식이 같으므로 #22가 붙으면 그대로 이어진다.
                target = f"{target_file}::{item.origin_name}"
            else:
                # 외부 라이브러리. 근거로만 남기고 그래프에는 올리지 않는다.
                target = f"{item.module}.{item.origin_name}"

            rows.append((path, item, source, target))
    return rows


def _update_reference_counts(db: Session, repo: Repo) -> None:
    """심볼별 피참조 횟수를 채운다.

    그래프가 15개를 넘으면 이 값으로 정렬해 자른다 (api-spec §4).
    그래프에 그려지는 연결만 센다. import는 근거로만 저장하고 노드로 만들지
    않으므로, 세면 화면에 보이는 개수와 숫자가 어긋난다.
    """
    counts = dict(
        db.execute(
            select(Reference.target_ident, func.count())
            .where(
                Reference.repo_id == repo.id,
                Reference.organization_id == repo.organization_id,
                Reference.ref_type != "import",
            )
            .group_by(Reference.target_ident)
        ).all()
    )
    for symbol in db.scalars(query(Symbol, repo.organization_id).where(Symbol.repo_id == repo.id)):
        symbol.reference_count = counts.get(symbol.ident, 0)


def parse_repo(db: Session, repo: Repo, repo_dir: Path) -> tuple[int, int]:
    """레포를 파싱해 저장하고 (심볼 수, 참조 수)를 돌려준다. 참조는 호출과 import 합이다.

    읽기·파싱을 먼저 다 끝내고, 저장은 마지막에 한 트랜잭션으로 한다.
    중간에 실패해도 이전 인덱스가 그대로 남아 화면이 비지 않는다.
    """
    files = list(_iter_files(repo_dir))
    repo.progress_current = 0
    repo.progress_total = len(files)
    db.commit()

    all_symbols: list[ParsedSymbol] = []
    all_references: list[ParsedReference] = []
    imports_by_file: dict[str, list[ParsedImport]] = {}
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
                parsed = adapter.parse(relative, content)
            except Exception:
                # 파일 하나가 파서를 넘어뜨려도 인덱싱 전체가 실패하면 안 된다.
                logger.exception("파싱 실패, 건너뜁니다: %s", relative)
            else:
                all_symbols.extend(parsed.symbols)
                all_references.extend(parsed.references)
                if parsed.imports:
                    imports_by_file[relative] = parsed.imports

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

    modules = _module_paths([path for path, _, _ in read_files])
    index = _build_import_index(imports_by_file, modules)

    resolved, skipped = _resolve_targets(all_references, symbols_by_name, index)
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

    import_rows = _import_references(imports_by_file, modules)
    for path, item, source_ident, target_ident in import_rows:
        db.add(
            Reference(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                source_ident=source_ident,
                target_ident=target_ident,
                ref_type="import",
                path=path,
                line=item.line,
            )
        )
    db.flush()

    _update_reference_counts(db, repo)

    repo.files_count = len(read_files)
    repo.functions_count = len(all_symbols)
    repo.progress_current = len(files)
    db.commit()

    logger.info(
        "파싱 완료 repo_id=%s 파일=%d 심볼=%d 호출=%d import=%d 건너뜀=%d",
        repo.id, len(read_files), len(all_symbols), len(resolved), len(import_rows), skipped,
    )
    return len(all_symbols), len(resolved) + len(import_rows)
