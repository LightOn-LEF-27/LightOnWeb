"""명령줄 — main() 안의 지역 변수가 모듈 이름을 가리면 그 모듈을 쓰는 명령이 전부 UnboundLocalError 로 죽는다."""

import ast
from pathlib import Path

from lefdata import cli


def test_main_does_not_shadow_the_modules_it_calls():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    imported = {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    stored = {n.id for n in ast.walk(main) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    assert not imported & stored
