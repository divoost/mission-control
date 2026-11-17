# Mission Control — 기능 정리 & 검증 가이드

> 목적: 지금까지 구현된 기능 전체와 **각 숫자가 어떤 실데이터에서 나오는지**, 그리고 **직접 검증하는 법**을 정리.
> 원칙: 모든 수치는 로컬 실데이터(git · Claude Code 로그). 못 가져오는 것은 **지어내지 않음**.

---

## A. 자동 감지 (zero-config · git 읽기 전용)

| 기능 | 데이터 출처 | 검증 방법 |
|---|---|---|
| 마일스톤 | 저장소 성숙도(문서/테스트/CI/컨테이너/릴리스) 또는 config | config의 `milestones[]`와 대조 |
| 단계(Phase) | 설정·품질 완성도 추정 | config의 `phases[]` |
| 기능 상태(완료/진행/대기) | config `features[]` | 직접 센 값과 일치 |
| 기술 스택 | package.json·파일 확장자 | `git ls-files`로 확인 |
| 코드 구성/모듈 구조 | 파일 확장자·최상위 폴더 집계 | `git ls-files` 집계와 대조 |
| DB 테이블 | `.sql`의 CREATE TABLE·`.prisma`의 model | 스키마 파일에서 직접 count |
| 화면/문서 | pages·app·screens 폴더, `.md` | 파일 목록과 대조 |
| 브랜치·기여자 | `git for-each-ref`·`git log %an` | 동일 git 명령 |
| 커밋 활동(14일)·작업 분포 | `git log` 날짜·커밋 태그 | `git log --since=14.days` |
| TODO/FIXME | `git grep -E 'TODO|FIXME|HACK'` | 동일 명령 |

**엔드포인트:** `/api/status?p=<id>` · `/api/overview`

---

## B. 실시간 작업 (Claude Code 로컬 세션 로그)

| 기능 | 데이터 출처 | 검증 방법 |
|---|---|---|
| 지금 작업중(세션별 주제) | `~/.claude/projects/*/*.jsonl`의 user 메시지 | 로그의 실제 프롬프트 |
| 최근 액션(수정/작성/실행…) | 로그의 assistant tool_use | 로그의 tool_use 블록 |
| 파일 편집 + ±라인 | Edit/Write의 old/new_string 줄수 | **세션 로그 기준 편집량** — git diff 최종값과 **다를 수 있음**(별개) |
| 편집 실시간 스트림 | 위 편집을 최신순 집계 | 동일(로그 기준) |
| 작업 상태(활성~3분/최근/유휴) | 로그 마지막 타임스탬프 **추론** | 최근 로그 시간 기준 **추정** — Claude가 실제 실행 중인지/입력 대기인지 **구별 못 함** |
| 토큰 사용량(입/출/캐시) | 로그 `message.usage` 합산 | jsonl의 usage 합계 |

**엔드포인트:** `/api/activity` · `/api/claude-usage`
**주의:** claude.ai **채팅** 프로젝트는 API가 없어 **여기에 안 나옴**(수동 보드로만).

---

## C. AI 프로젝트 관리 레이어 (최근 추가 · 제품평가 반영)

| 기능 | 데이터 출처 | 검증 방법 / 주의 |
|---|---|---|
| **기능(등록) 완료율** | 등록된 기능 done/total | config features의 done ÷ 전체. **코드·테스트를 자동 검증한 값이 아님** — 라벨도 "기능 완료율"로 표기 |
| ↳ git 추정과 **분리 표기** | 도넛·타일·카드에 "기능 완료율 N% · git 추정 M%" | 둘이 다름을 눈으로 확인 |
| **개발 보고서** | 오늘/7일 커밋·변경 파일(git)·열린 TODO | `git log --since` 와 대조 |
| **다음 할 일** | 진행중/미착수 기능·코드 TODO·미완료 마일스톤 | **실제 신호만**(지어내지 않음) |
| **작업 상태 추론** | 세션 로그 최근 활동 | 추정 — 실행/입력대기 **구별 못 함**(B와 동일) |

**엔드포인트:** `/api/report`

---

## D. 종합(Overview) 대시보드

- 전체 집계(프로젝트·커밋·기능 완료율·기능·에이전트) + 프로젝트별 카드
- 개발 지표 링: 프로젝트별 세부 ↔ 종합으로 **순환**
- 콘솔 로그: 실제 DB 테이블·모듈·화면·문서가 올라감
- 기타 리포(자동) · 내 프로젝트(수동, opt-in) 섹션 구분

---

## E. 선택 기능 (opt-in · 기본 꺼짐)

| 기능 | 켜는 법 | 비고 |
|---|---|---|
| Anthropic API 토큰·비용 | config `anthropicAdminKey` | Admin 키 필요(로컬·gitignore) |
| 내 프로젝트 수동 보드 | `my-projects.json` 생성 | claude.ai 등 비-git 작업용, "직접 기록" 표기 |
| 활동 주제 숨김 | config `activityTopics:false` | 액션·파일변경만 노출 |

---

## F. UX · 연출

- **6개 언어**(EN/KO/VI/JA/ES/ZH) · **4 테마**(cyan/amber/matrix/magenta)
- 모션: 우주 도넛(입자·연결선·혜성·숨쉬는 글로우), 지표 링 순환, 이퀄라이저, 마퀴(속도·크기 차별), 콘솔 로그 스크롤
- **화려한 연출은 의도적 — 숫자는 전부 진짜**

---

## G. 정직성 원칙 (핵심 차별점)

1. 모든 수치 = 로컬 실데이터. 못 가져오는 것(claude.ai 채팅, 타 AI 도구)은 **표시하지 않음**.
2. **기능(등록) 완료율 ↔ git 추정**을 나란히 → 과대평가 오해 방지. (완료율은 등록 기능 기준이며 코드·테스트 자동 검증이 아님)
3. 다음 할 일 = 실제 신호(기능·TODO·마일스톤)에서만. AI 추측 금지.
4. 각 패널에 출처 뱃지: `REAL · LOCAL` / `REAL · GIT` / `REAL · API`.
5. 보안: `127.0.0.1`만 바인드, git 구조·토큰 수만 읽고 **`.env`·키·시크릿 절대 안 읽음**, 외부 전송 없음.

### ⑤ 타 AI(Codex 등) 연동 — 현재 보류 (정직)
- 이유: 이 컴퓨터의 `~/.codex`는 **앱 내부 디버그 로그**(level/file/line)뿐, 프로젝트별 작업 내용이 없음.
- Cursor/Aider 등 읽을 로그도 없음 → **가짜로 지어내지 않고 보류.**
- 로그 리더는 "여러 도구 지원" 구조로 두어, 읽을 로그가 생기면 붙임.

---

## 검증 빠른 체크 (직접 해보기)

```bash
# 서버 실행 중일 때 (127.0.0.1:8799)
curl -s 127.0.0.1:8799/api/overview | python3 -m json.tool   # 실제완료율 vs git추정
curl -s 127.0.0.1:8799/api/report   | python3 -m json.tool   # 다음 할 일·보고서
curl -s 127.0.0.1:8799/api/activity | python3 -m json.tool   # 세션·파일변경·상태
```
- 기능(등록) 완료율 = config의 features done 개수 ÷ 전체. (코드·테스트 검증 아님)
- 다음 할 일 항목 = config의 wip/todo 기능 + 코드 TODO와 일치.
- 보고서 '변경 파일' = `git -C <repo> log --since=7.days --name-only` 와 대조.
- 세션 '편집 ±라인' = Edit/Write 로그 기준 **편집량**이라 git diff 최종값과 다를 수 있음(별개 지표).
