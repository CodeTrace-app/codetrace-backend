"""코드 탐색기 API (api-spec §4). 파일 트리·파일 내용·영향 범위 그래프.

그래프는 파서가 확정한 참조만 쓴다. 추측으로 만든 연결은 없다.
없는 관계를 보여주지 않는 것이 이 제품의 주장이므로, 확정되지 않은 것은 애초에
저장되지 않았고 여기서도 만들어내지 않는다.
"""

import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.context import record_query_log
from src.auth import Ctx, current_user, get_db
from src.db.models import Reference, Repo, SourceFile, Symbol
from src.db.query import query
from src.indexing.parsing import MAX_FILE_BYTES
from src.schemas import (
    FileFunctionOut,
    FileOut,
    GraphEdgeOut,
    GraphNodeOut,
    GraphOut,
    GraphRootOut,
    TreeDirOut,
    TreeFileOut,
    TreeOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["explorer"])

# 영향 범위는 깊이 2 고정 (S-QGBSHN). 더 들어가면 화면이 읽을 수 없어진다.
GRAPH_DEPTH = 2

# 서버가 한 번에 내려주는 노드 상한. 넘으면 잘라내고 truncated로 알린다 (api-spec §9).
# 15개 초과 접기는 프론트 몫이고, 이건 응답 크기를 막는 상한이다.
MAX_GRAPH_NODES = 100


def _get_repo(repo_id: int, ctx: Ctx, db: Session) -> Repo:
    repo = db.scalar(query(Repo, ctx.organization_id).where(Repo.id == repo_id))
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "레포를 찾을 수 없습니다")
    return repo


# ---------------------------------------------------------------- 파일 트리


def _ensure_dir(path: str, dirs: dict[str, TreeDirOut], root: list) -> TreeDirOut:
    """디렉터리 노드를 만든다. 상위 디렉터리가 없으면 그것부터 만든다."""
    existing = dirs.get(path)
    if existing is not None:
        return existing

    node = TreeDirOut(path=path, name=path.rsplit("/", 1)[-1], children=[])
    dirs[path] = node
    if "/" in path:
        _ensure_dir(path.rsplit("/", 1)[0], dirs, root).children.append(node)
    else:
        root.append(node)
    return node


def _sort_tree(nodes: list) -> None:
    """디렉터리를 먼저, 그다음 이름순. 탐색기에서 익숙한 순서다."""
    nodes.sort(key=lambda n: (n.type != "dir", n.name.lower()))
    for node in nodes:
        if isinstance(node, TreeDirOut):
            _sort_tree(node.children)


def _build_tree(files: list[SourceFile]) -> list:
    dirs: dict[str, TreeDirOut] = {}
    root: list = []

    for file in sorted(files, key=lambda f: f.path):
        node = TreeFileOut(path=file.path, name=file.path.rsplit("/", 1)[-1], language=file.language)
        if "/" in file.path:
            _ensure_dir(file.path.rsplit("/", 1)[0], dirs, root).children.append(node)
        else:
            root.append(node)

    _sort_tree(root)
    return root


@router.get("/{repo_id}/tree")
def get_tree(
    repo_id: int, ctx: Ctx = Depends(current_user), db: Session = Depends(get_db)
) -> TreeOut:
    """좌측 파일트리. 인덱싱된 default_branch 기준."""
    repo = _get_repo(repo_id, ctx, db)
    if repo.indexing_status != "done":
        raise HTTPException(status.HTTP_409_CONFLICT, "인덱싱이 끝나지 않았습니다")

    files = list(
        db.scalars(query(SourceFile, ctx.organization_id).where(SourceFile.repo_id == repo.id))
    )
    return TreeOut(root=_build_tree(files))


# ---------------------------------------------------------------- 파일 내용


@router.get("/{repo_id}/file")
def get_file(
    repo_id: int,
    path: str = Query(..., min_length=1, max_length=500),
    ctx: Ctx = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileOut:
    """중앙 코드뷰어. 읽기 전용."""
    repo = _get_repo(repo_id, ctx, db)
    file = db.scalar(
        query(SourceFile, ctx.organization_id)
        .where(SourceFile.repo_id == repo.id)
        .where(SourceFile.path == path)
    )
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다")

    symbols = db.scalars(
        query(Symbol, ctx.organization_id)
        .where(Symbol.repo_id == repo.id)
        .where(Symbol.path == path)
        .where(Symbol.kind == "function")
    )

    return FileOut(
        path=file.path,
        language=file.language,
        content=file.content,
        # 인덱싱이 상한까지만 읽어 저장한다. 저장된 크기가 상한에 닿아 있으면 잘린 것이다.
        truncated=len(file.content.encode("utf-8")) >= MAX_FILE_BYTES,
        functions=sorted(
            (
                FileFunctionOut(name=s.name, start_line=s.start_line, end_line=s.end_line)
                for s in symbols
            ),
            key=lambda f: f.start_line,
        ),
    )


# ---------------------------------------------------------------- 영향 범위 그래프


def _neighbours(
    db: Session, ctx: Ctx, repo: Repo, symbols: dict[str, Symbol]
) -> tuple[dict[str, list], dict[str, list]]:
    """참조를 양방향 인접 목록으로 바꾼다. (이 심볼을 부르는 쪽, 이 심볼이 부르는 쪽)

    양쪽 끝이 모두 실제 심볼인 참조만 쓴다. import 근거는 출발점이 파일(::<module>)이라
    여기서 자연히 빠진다 — 그래프 노드 kind는 function|constant|class뿐이고,
    외부 라이브러리를 노드로 만들면 프로젝트 밖을 그리게 된다 (이슈 #20 결정).
    """
    callers: dict[str, list] = defaultdict(list)
    callees: dict[str, list] = defaultdict(list)

    references = db.scalars(
        query(Reference, ctx.organization_id).where(Reference.repo_id == repo.id)
    )
    for ref in references:
        if ref.source_ident not in symbols or ref.target_ident not in symbols:
            continue
        callers[ref.target_ident].append((ref.source_ident, ref.ref_type))
        callees[ref.source_ident].append((ref.target_ident, ref.ref_type))

    return callers, callees


def _node_of(symbol: Symbol, depth: int, direction: str) -> GraphNodeOut:
    return GraphNodeOut(
        id=symbol.ident,
        name=symbol.name,
        path=symbol.path,
        kind=symbol.kind,
        depth=depth,
        direction=direction,
        reference_count=symbol.reference_count,
    )


def _build_graph(root: Symbol, symbols: dict[str, Symbol], callers, callees) -> GraphOut:
    """root에서 양방향으로 깊이 2까지 훑는다.

    이미 담은 노드는 다시 담지 않는다. 순환 참조가 있어도 같은 노드가 두 번 나오거나
    무한히 도는 일이 없다. 다만 간선은 양쪽 끝이 모두 담긴 것이면 남긴다 —
    노드가 중복이라고 연결까지 지우면 영향 범위가 실제보다 좁아 보인다.
    """
    visited = {root.ident}
    nodes: list[GraphNodeOut] = []
    edges: list[GraphEdgeOut] = []
    edge_keys: set[tuple[str, str, str]] = set()
    truncated = False

    frontier: list[tuple[str, str | None]] = [(root.ident, None)]

    for depth in range(1, GRAPH_DEPTH + 1):
        found: list[tuple[str, str, GraphEdgeOut]] = []
        for ident, direction in frontier:
            for source_ident, ref_type in callers[ident]:
                edge = GraphEdgeOut(source=source_ident, target=ident, type=ref_type)
                found.append((source_ident, direction or "caller", edge))
            for target_ident, ref_type in callees[ident]:
                edge = GraphEdgeOut(source=ident, target=target_ident, type=ref_type)
                found.append((target_ident, direction or "callee", edge))

        # 많이 참조되는 것부터 담는다. 상한에 걸려 잘릴 때 남는 쪽이 더 쓸모 있다.
        found.sort(key=lambda item: -symbols[item[0]].reference_count)

        next_frontier: list[tuple[str, str | None]] = []
        for ident, direction, edge in found:
            if ident not in visited:
                if len(nodes) >= MAX_GRAPH_NODES:
                    truncated = True
                    continue
                visited.add(ident)
                nodes.append(_node_of(symbols[ident], depth, direction))
                next_frontier.append((ident, direction))

            key = (edge.source, edge.target, edge.type)
            if key not in edge_keys and edge.source in visited and edge.target in visited:
                edge_keys.add(key)
                edges.append(edge)

        frontier = next_frontier

    return GraphOut(
        root=GraphRootOut(id=root.ident, name=root.name, path=root.path, kind=root.kind),
        nodes=nodes,
        edges=edges,
        total_nodes=len(nodes),
        truncated=truncated,
    )


@router.get("/{repo_id}/graph")
def get_graph(
    repo_id: int,
    path: str = Query(..., min_length=1, max_length=500),
    function: str = Query(..., min_length=1, max_length=200),
    ctx: Ctx = Depends(current_user),
    db: Session = Depends(get_db),
) -> GraphOut:
    """우측 탭2 "영향 범위". 파서 결과만 사용 — 추측 없음."""
    repo = _get_repo(repo_id, ctx, db)

    symbols = {
        s.ident: s
        for s in db.scalars(
            query(Symbol, ctx.organization_id).where(Symbol.repo_id == repo.id)
        )
    }
    root = symbols.get(f"{path}::{function}")
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "함수를 찾을 수 없습니다")

    callers, callees = _neighbours(db, ctx, repo, symbols)
    response = _build_graph(root, symbols, callers, callees)

    record_query_log(db, ctx, repo, root.ident, action="graph_view")
    return response
