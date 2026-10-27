"""테스트 공통 — 기대값 파일에서 한 번도 안 쓴 값을 끝에 알려 준다 (그 값을 보던 검사가 빠졌을 수 있다).

같은 파일을 여러 테스트가 따로 열 수 있으므로 파일마다 모아서 본다. 일부 테스트만 돌리면 당연히 안 쓴 값이 생기므로
실패로 하지 않고 알리기만 한다.
"""

import expected


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    roots = {expected.ROOT.resolve(), expected.EXAMPLE_ROOT.resolve()}        # 테스트가 임시로 만든 파일은 빼고
    by_file: dict = {}
    for e in expected.opened():
        if e.exists and e.path.parent.resolve() in roots:
            first, used = by_file.setdefault(e.path.resolve(), (e, set()))
            used |= e.used
    left = []
    for first, used in by_file.values():
        first.used = used
        left += [(first.path.name, k) for k in first.unused()]
    if left:
        terminalreporter.write_sep("-", f"기대값 파일에서 쓰지 않은 값 {len(left)}개 (검사가 빠졌나? 일부만 돌렸으면 정상)")
        for name, key in left:
            terminalreporter.write_line(f"  {name}: {key}")
