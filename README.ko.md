# cursor-handoff

합의된 구현 계획을 **플래너/리뷰어**(Codex 또는 Claude Code)가 **Cursor CLI**에 넘겨 실행하게 하고, 실제 diff와 테스트 증거를 검토하는 포터블 스킬·러너입니다.

선택적 **Claude consult**는 사용자가 Claude 논의/리뷰를 요청할 때만 쓰는 독립 비판 단계입니다. Claude는 구현 실행기가 아닙니다.

```text
  Codex / Claude Code          Claude CLI (선택)          Cursor CLI           워크스페이스
  (계획 + 종합)    --brief-->  (비판만)                   (편집 + 실행) --기록--> 소스/테스트
        |                        advice.md                     |
        |                        (신뢰 불가)                   |
        +----- 결정 기록 / 동결 task.md ----------------------+
        ^                                                       |
        +----- result.json / status.json / diff 증거 ----------+
```

실행기는 항상 Cursor CLI입니다. Claude.ai 웹 채팅만으로는 로컬 CLI를 실행할 수 없습니다.

## 사전 요구 사항

- Python 3.10+
- `handoff.py`를 돌리는 머신에 Cursor agent CLI 설치 및 로그인
- 선택: 사용자가 Claude 논의를 요청할 때 `consult.py`용 Claude Code CLI
- 스킬을 지원하는 플래너 호스트(Codex 또는 Claude Code)

이 패키지는 Cursor/Claude 자격 증명, API 키, 모델 구독을 포함하지 않습니다.

## 플랫폼

| 플랫폼 | 지원 대상 | 실기 검증 상태 |
| --- | --- | --- |
| Windows | 예 | 별도 toy 워크스페이스에서 포터블 Cursor 스모크 검증(함수 생성, assertion 2개, `execution_completed`, 잠금 해제) |
| Linux | 예 | 여기서 실기 미검증; CI 매트릭스 예정 |
| macOS | 예 | 여기서 실기 미검증; CI 매트릭스 예정 |

단위 테스트는 가짜 Cursor/Claude CLI를 쓰며 Linux/macOS 실기 CLI 동작을 증명하지 않습니다.

**Claude 실제 advice:** UNVERIFIED — 라이브 consult가 `is_error=true`(주간 쿼터)를 반환했습니다. 오프라인 fake-Claude 테스트를 실기 advice 검증으로 취급하지 마세요.

릴리스 압축을 푼 뒤 `python install.py ...`로 설치할 수 있습니다. clone quickstart는 `https://github.com/Lunecid/cursor-handoff` 후 `cd cursor-handoff`입니다.

## 설치

저장소를 clone하거나 릴리스 압축을 푼 뒤 스킬을 설치합니다(로컬 복사만 수행; Cursor/Claude/Python은 설치하지 않음):

```bash
git clone https://github.com/Lunecid/cursor-handoff
cd cursor-handoff
python install.py --target both --scope user
```

예시:

```bash
# Codex 사용자 스킬(문서화된 기본: ~/.agents/skills)
python install.py --target codex --scope user

# 예전 Codex 호스트(~/.codex/skills)
python install.py --target codex --scope user --destination ~/.codex/skills

# Claude Code 사용자 스킬
python install.py --target claude --scope user

# 프로젝트 범위 설치
python install.py --target both --scope project --project /path/to/repo

# 기존 설치 교체(.cursor-handoff-backups/<id>/ 아래에 백업)
python install.py --target codex --scope user --update
```

기본값은 기존 스킬 디렉터리를 덮어쓰지 않으며, `--update`가 있을 때만 교체합니다. 업데이트 백업은 숨김 `.cursor-handoff-backups/<unique-id>/`에 두어 `SKILL.md`가 다른 스킬로 발견되지 않게 합니다.

## 사용법

### 선택적 Claude 논의(비판만)

사용자가 Claude 논의/리뷰를 요청할 때만 사용합니다. 기본은 최대 두 번 consult입니다. 호스트가 **Claude Code 자체**이면 자기 자신에 대해 자동 consult를 돌리지 말고, 명시적인 독립 리뷰 요청일 때만 실행하세요.

```bash
python skills/cursor-handoff/scripts/consult.py \
  --workspace /path/to/project \
  --brief /path/to/project/design-brief.md
```

유용한 플래그: `--timeout 300`, `--model`, `--claude-path` / `CLAUDE_CODE_PATH`, `--dry-run`, `--doctor`.

산출물은 `.cursor-handoff/consult-<id>/`에 저장됩니다(`brief.md`, `response.json`, `advice.md`, `stderr.log`, `status.json`, 일회성 settings/MCP 설정). Claude 프로세스 cwd는 워크스페이스 **밖**의 러너 소유 임시 디렉터리이며(종료 후 정리), 홈/관리 정책은 여전히 적용될 수 있습니다. 성공 상태는 **`advice_received`**(합의 아님)입니다. Cursor 작업을 동결하기 전에 채택/거절 항목을 기록하세요. `examples/design-brief.md` 참고.

쿼터·로그인 실패는 코디네이터 차단 사유이며, 모델/제공자를 자동 전환하지 마세요.

### Cursor 구현 핸드오프

1. Codex(`$cursor-handoff`) 또는 Claude Code(`/cursor-handoff`)에서 계획을 합의합니다(선택적으로 consult 종합 후).
2. 대상 워크스페이스 안에 UTF-8 작업 파일을 작성합니다(`examples/task.md` 참고).
3. 실행:

Codex·Claude Code 토큰을 아끼려면 코드 탐색과 검증은 Cursor에 맡기고, 설계자는 작업 범위에 필요한 규칙·맥락만 읽은 뒤 Cursor의 간결한 결과와 관련 변경 diff를 검토합니다. 오류나 위험이 있을 때만 전체 로그를 펼칩니다. Windows에서는 `--live` 없이 `--open-terminal`만 쓰면 진행 문구가 설계자의 대화로 들어오지 않고 별도 창에 표시됩니다. Claude 논의는 요청 시에만 수행하며 Claude 토큰을 사용합니다. 총 토큰 또는 비용 절감은 보장되지 않습니다.

```bash
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md
```

유용한 플래그:

- `--timeout 600` — 프로세스 트리 종료 시한
- `--model <id>` — 선택적 Cursor 모델
- `--agent-path` / `CURSOR_AGENT_PATH` — 실행 파일 또는 Windows `agent.ps1`
- `--trust-workspace` — 해당 워크스페이스에 Cursor `--trust`를 의도적으로 전달(기본 off). `.workspace-trusted` 마커가 남을 수 있고 하위 디렉터리가 상속할 수 있음 — 조용한 일회성(per-run) 스위치가 아니며 자동으로 켜지 않음
- `--live` — stderr에 사람용 진행 상황(워크스페이스, task, trust 플래그, pid/session, 도구 요약, stderr 오류, 최종 상태); stdout JSON 호환 유지
- `--open-terminal` — Windows: Cursor 전에 `watch.py`를 별도 콘솔로 실행; 다른 OS: 수동 watch 명령을 출력하고 live 진행 활성화
- `--dry-run` — 검증·계획만 출력; run/lock/변경 없음
- `--doctor` — 설치/기능 보고; 로그인·설정 쓰기 없음

가시적 관찰(관찰 전용; trust 프롬프트에 응답할 수 없음):

```bash
# Windows: 별도 뷰어 콘솔; 설계자 출력은 간결하게 유지
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md \
  --open-terminal

# Linux/macOS: live 진행; 필요 시 수동 뷰어
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md \
  --live
python skills/cursor-handoff/scripts/watch.py --run /path/to/project/.cursor-handoff/<run-id>
```

산출물은 `.cursor-handoff/<run-id>/`에 저장됩니다(`task.md`, `events.jsonl`, `stderr.log`, `result.json`, `status.json`). 러너는 Cursor가 동결된 `run/task.md` 스냅샷을 읽도록 가리킵니다. 로그는 비공개로 취급하세요. CLI의 `execution_completed`는 지원되는 성공 result(`type=result`, `subtype=success`, `is_error=false`)로 끝났다는 뜻이며, 작업 결과의 자동 리뷰 승인이 아닙니다. 명시적 workspace-trust 거부는 `blocked` / `trust_required`(비정상 종료)로 기록됩니다. 이미 trust를 거부한 경우 그 결정을 우회하지 마세요. trust가 아직 설정되지 않았다면 해당 워크스페이스에만 `--trust-workspace`로 의도적으로 다시 실행하거나(Cursor가 `.workspace-trusted`를 남길 수 있음) Cursor 대화형 trust 설정을 완료하세요. 러너는 `--trust`를 자동으로 켜지 않습니다.

## 오프라인 점검

```bash
python skills/cursor-handoff/scripts/handoff.py --doctor
python skills/cursor-handoff/scripts/consult.py --doctor
python skills/cursor-handoff/scripts/handoff.py --workspace . --task examples/task.md --dry-run
python skills/cursor-handoff/scripts/consult.py --workspace . --brief examples/design-brief.md --dry-run
```

## 검증

```bash
python -m unittest discover -s tests -v
```

기본 테스트는 mock 런처를 쓰며 실제 콘솔 창을 열지 않습니다. 실제 Windows GUI 핸드셰이크(`test_windows_viewer_console_handshake`)는 옵트인입니다:

```powershell
# Windows 전용; CREATE_NEW_CONSOLE 뷰어 창을 엽니다
$env:CURSOR_HANDOFF_TEST_GUI = '1'
python -m unittest discover -s tests -p test_handoff.py -k test_windows_viewer_console_handshake -v
```

## 릴리스 패키징

```bash
python scripts/package_release.py
```

결정적 `dist/cursor-handoff-<version>.zip`과 `.sha256`을 만듭니다. 압축을 풀면 `python install.py ...`로 바로 설치할 수 있습니다.

## 문제 해결

| 증상 | 조치 |
| --- | --- |
| agent를 찾지 못함 / PATH 오래됨 | `--doctor`, `--agent-path`, 또는 `CURSOR_AGENT_PATH`; Windows는 `%LOCALAPPDATA%\cursor-agent\agent.ps1` 확인 |
| Claude CLI를 찾지 못함 | `consult.py --doctor`, `--claude-path`, 또는 `CLAUDE_CODE_PATH`; Windows는 네이티브 `.exe` 권장 |
| trust/권한 프롬프트 | 의도할 때만 `--trust-workspace` (`.workspace-trusted`가 남을 수 있음); 워크스페이스 경로는 OS 샌드박스가 아님. 뷰어는 trust 프롬프트에 응답할 수 없음. `blocked`/`trust_required`는 명시적 거부 — 자동 재시도·거부 우회 금지 |
| 로그인/쿼터 필요 | 호스트에서 해당 CLI 로그인; 우회하거나 제공자 자동 전환 금지 |
| 타임아웃 | `--timeout` 상향; `stderr.log`와 `status.json`(`timeout` 유지) 확인 |
| 잠금 존재 | 다른 handoff/consult가 실행 중이거나 `.cursor-handoff/workspace.lock`이 남음 — 러너 없음을 확인한 뒤 수동 삭제 |

## 공식 문서

- [Cursor CLI headless](https://cursor.com/docs/cli/headless)
- [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code headless](https://code.claude.com/docs/en/headless)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [ChatGPT/Codex skills](https://learn.chatgpt.com/docs/build-skills)

## 라이선스

MIT — `LICENSE` 참고.
