# pg_acorn 빌드 성능 백로그

> 작성: 2026-06-15
> 근거: Cohere 10M 규모 벤치(`bench/REPORT_scale.md`)에서 측정한 빌드 시간 + `pg_stat_progress_create_index`/`top` 관측 + `src/acorn_build.c` 코드 분석
> 목적: acorn 인덱스 빌드 시간을 줄이는 구조적(알고리즘) 작업을 우선순위와 함께 정의한다.

---

## 측정된 문제 (baseline)

| N | pgvector hnsw | acorn g2p64 | acorn/pgv |
|---|---|---|---|
| 100K | 70s / 0.8GB | 308s / 0.8GB | 4.4x |
| 1M | 164s / 7.8GB | 458s / 8.4GB | 2.8x |
| 10M | 1500s(25분) / 76GB | **5201s(87분)** / 82GB | 3.5x |

빌드 설정: m=16, ef_construction=64, acorn_gamma=2, acorn_payload_m=64, non-inline, mwm 64GB, max_parallel_maintenance_workers=30, n2-standard-32(32 vCPU).

### 어디서 시간이 가는가 (관측 + 코드 근거)

1. **거리 계산의 절대량.** 노드당 `m_eff = m×gamma = 32` 기본 이웃 + `payload_m = 64` payload 이웃 = 최대 96개(`acorn_payload_m_eff`, `acorn_build.c:210`). pgvector(layer0 ~32)의 ~3배. 거리 커널은 이미 fmgr 우회 + SIMD(`acorn_build.c:228-231`)라 op당은 빠르지만 **op 수가 3배** → acorn/pgv 비율의 본질.
2. **락 경합.** 10M 빌드 중 `top`에서 **CPU 88% idle**(user 0%, sys ~8%) 관측 — CPU/IO 병목이 아니라 워커 대기. 워커를 16→30으로 올려도 wall-time 동일(100K 실측) → **병렬도가 ~16코어에서 포화**, vCPU 증설은 빌드에 무의미. 두 글로벌 락이 원인:
   - `allocatorLock`을 **노드 할당마다 LW_EXCLUSIVE** 취득(`acorn_build.c:1088-1102`; arena bump + node-id, `:910-911`) → N(=10M)번 직렬화.
   - `entryLock`을 **insert 전 구간 LW_SHARED 보유**(`acorn_build.c:1704-1706, 1731`); entry 갱신은 LW_EXCLUSIVE(`:1740`)라 모든 in-flight insert를 대기.

### 선행 측정 (코드 착수 전 게이트)

설정 Pareto부터 측정해 "어디까지가 설정으로 해결되는지" 분리한다. VM에 1M 적재본이 있으면 빌드당 ~5-8분:
`acorn_payload_m ∈ {16,32,64}` × `workers ∈ {16,30}`, 빌드 시간 + sel{1,10}% filtered recall 기록.
- 가설 1: workers=16이 30과 동일/우세(경합↓) → 기본값을 16으로.
- 가설 2: payload_m=32가 빌드 ~30%↓ + recall 손실 미미 → 기본값 재고.
이 결과가 아래 알고리즘 작업의 ROI 기준선이 된다.

---

## 알고리즘 백로그 (장기 최대 효과)

| # | 항목 | 적용 위치 | 기대 효과 | 난이도 | 위험 |
|---|------|----------|----------|--------|------|
| B1 | allocatorLock 제거 (워커별 arena 청크) | `acorn_build.c:1088-1102` | 병렬도 회복 (idle↓) | 중 | 낮음 |
| B2 | entryLock 임계구간 단축 | `acorn_build.c:1704-1760` | exclusive 갱신 stall 완화 | 소 | 낮음 |
| B3 | payload edge 2-패스 분리 | `acorn_build.c` 빌드 경로 | acorn 고유 비용 격리·병렬화 | 대 | 중 |

### B1. allocatorLock 제거 — 워커별 arena 청크

현재 모든 노드 할당이 글로벌 `allocatorLock`을 exclusive로 잡는다(`acorn_build.c:1088`). 10M 노드 = 10M번 직렬화.

**개선:** 워커가 락을 **한 번** 잡아 arena 블록 + node-id 범위(예: 4K개)를 예약하고, 이후 그 청크 안에서 **로컬 lock-free** 할당. 청크 소진 시에만 다시 락. N번 → O(N/청크크기 × 워커수)번 취득.

**검증:** 빌드 중 `top` CPU idle% 감소 + wall-time 단축. `pg_stat_progress_create_index` tuples_done 증가율로 비교.

### B2. entryLock 임계구간 단축

현재 entry point를 insert 전 구간 동안 LW_SHARED로 보유(`acorn_build.c:1704-1706, 1731`). entry 갱신(LW_EXCLUSIVE, `:1740`)이 모든 in-flight insert를 기다려 stall.

**개선:** entry id를 **원자적 스냅샷**으로 읽고 즉시 해제(insert 본체는 스냅샷 id로 진행). 갱신은 CAS/짧은 exclusive로. `entryWaitLock` 기아 방지 로직은 유지.

**검증:** 상위 레이어 노드가 늘어나는 초반 구간에서 stall 감소. 단독으로는 효과가 작을 수 있어 B1과 함께 측정.

### B3. payload edge 2-패스 분리 — 구조적 최대 레버

현재 payload 이웃(64개)을 기본 그래프 삽입에 인터리브 → 삽입 순서 의존 + entryLock/allocatorLock 경합에 함께 묶임.

**개선:**
1. **1패스:** 기본 HNSW(m_eff=32)만 pgvector처럼 병렬 빌드.
2. **2패스:** 완성된 기본 그래프에서 **노드별 독립**으로 payload 이웃 계산. 삽입 순서 의존 없음 → entryLock 불필요, 노드 단위 분할로 embarrassingly-parallel. 결과를 노드 락만으로 기록.

acorn 고유 작업(payload edge)을 격리해 병렬도를 회복하고, 1패스를 pgvector 빌드 속도에 수렴시킨다. **acorn/pgv 3.5x 격차를 직접 겨냥하는 가장 큰 구조 변경.**

**위험/검증:** 2패스 payload 이웃이 1패스 인터리브와 **동일 품질(필터 recall)**인지 확인 필요 — 인터리브는 부분 그래프 상태에서 이웃을 고르지만 2패스는 완성 그래프에서 고르므로 오히려 품질↑ 가능. 빌드 시간 + sel{1,10}% recall로 동치성 검증. `build_seed=42` 고정 재현.

---

## 비레버 (이미 적용/무의미)

- fmgr 우회 + SIMD 거리 커널(`acorn_build.c:228-231`) — 적용됨.
- in-memory 빌드(mwm=64GB, spill 없음) — 적용됨.
- **vCPU 증설** — 병렬도가 ~16코어 포화라 빌드엔 무의미(실측). 단, B1/B2로 경합을 풀면 그때는 코어가 효과.

> 우선순위: 선행 측정(설정 Pareto) → B1+B2(경합) → 효과 측정 → B3(구조). [[development-roadmap.md]]의 Phase 0(측정·안전)과 같은 "측정 우선" 원칙을 따른다.

---

## 측정 결과 (2026-06-16, 10M Cohere, n2-standard-32, 30 workers, mwm=64GB)

브랜치 `feat/build-perf-atomic`(B1+B2+B3a+B3b 구현, docker-test green)을 VM에서 빌드해 10M acorn 인덱스
빌드 시간을 측정. **모든 체크포인트는 정확성(recall) 검증 통과**했으나, **빌드 시간은 의도대로 개선되지 않음** —
측정이 두 구현 단순화의 결함을 잡아냄.

| 빌드 | wall-time | CPU idle | 비고 |
|------|-----------|----------|------|
| OLD (baseline, f0a3308) | 5201s (87분) | 88% | LWLock 경합으로 코어 유휴 |
| NEW B1+B2 (two_pass off) | **5874s (98분)** | 16% | **~13% 역효과** |
| NEW B3 (two_pass on) | pass-1 **~19분** + pass-2 직렬 꼬리(>40분, 취소) | pass-2 94% | pass-1 5배↑, pass-2 부하 불균형 |

### 발견 1 — B1+B2 글로벌 atomic은 스케일에서 역효과
idle 88%→16%(코어 포화)이나 wall-time은 오히려 증가. 30코어가 공유 `n_nodes`/`arena_used` atomic에
fetch-add하며 **캐시라인 핑퐁** → LWLock-sleep보다 느림. **→ B1을 글로벌 atomic이 아니라 원래 설계의
"워커별 arena 청크"(락 1회로 블록 예약 후 로컬 lock-free 할당)로 구현해야 공유 카운터 바운싱을 없앤다.**
(현 구현은 정확하나 perf-neutral~negative — 롤백 또는 청크 방식 재구현 대상.)

### 발견 2 — B3 two-pass: 구조는 입증, pass-2 부하 불균형이 병목
pass-1(기본 그래프, 이웃 32개)이 **~19분**으로 인터리브 insert(~90분)의 **5배 빠름** — 2-패스 구조의
잠재력 입증. 그러나 pass-2가 사실상 직렬(2코어, 나머지 30 유휴):
- 활성 백엔드 32개(워커 살아있음)이나 100% CPU는 2개 → work-stealing은 분배되나 **노드별 pass-2 비용이
  극단적으로 불균형**.
- 원인: 상관 픽스처의 **거대 파티션**(우세 임베딩 블록 ~1M 멤버). `acorn_mem_search_partition`이
  1M-멤버 파티션 노드에서 거대 서브그래프를 순회 → 그 노드들이 work-stealing 꼬리를 직렬화.
- **→ 후속 작업:** (a) pass-2 partition search에 방문 상한(ef cap/샘플) 도입, (b) 거대 파티션 노드를
  서브청크로 쪼개 분산, (c) P4 게이팅에 **최대 카디널리티 상한** 추가(현재 `payload_min_card`는 최소만) —
  과대 파티션은 payload edge를 글로벌-only로 폴백.

### 교훈
- **CPU idle%가 핵심 진단**: B1/B2는 idle-wait(LWLock)을 busy-spin(atomic)으로 바꿨을 뿐 빨라지지 않음;
  B3 pass-2의 94% idle은 직렬 꼬리를 폭로.
- 소규모 parity 테스트(`tier2_build_two_pass.sql`)는 정확성은 보장하나 **병렬 효율은 검증 못 함** — 부하
  불균형은 대규모 + 상관 데이터에서만 드러남. 정확성 게이트와 **별도로 perf 회귀 측정**이 필요.
- 계획의 "코드 먼저, 측정으로 검증"이 정확히 이 결함들을 잡음 — default-off GUC라 휴면 상태로 안전하게 ship.

---

## 2차 반복 (2026-06-16, 측정 1차 결함 → N2/N1 수정)

1차 측정이 짚은 두 결함을 각각 독립 체크포인트로 수정. **모든 신규 knob default-off**(기존 동작 무변),
각 체크포인트 docker-test green(25 + 4), 단독 `git revert` 가능. 측정은 VM 재실행 예정(이번 라운드는 코드만).

### N2 — pass-2 partition-search 비용 한정 (`90353ee`)
발견 2의 pass-2 직렬 꼬리(거대 비선택 파티션) 대응. 두 보완 장치:
- **방문 상한 GUC `pg_acorn.build_payload_visit_cap`**(int, default 0=무한): `acorn_mem_search_partition`이
  파티션 멤버에 거리계산한 횟수가 cap 초과 시 break. 바닥값 `Max(cap, ef)`로 결과창 미만 굶김 방지.
  거대 파티션 1개가 pass-2를 직렬화하지 못하게 노드별 비용을 유계화.
- **최대-카디널리티 게이트 reloption `acorn_payload_max_cardinality`**(per-index, `payload_min_cardinality`
  대칭, default 0=off): 멤버 수 > 상한인 과대 파티션은 payload edge를 건너뛰고 글로벌-only 폴백. 거대
  비선택 파티션(10%↑ 통과)은 글로벌 그래프로 충분하므로 **결과-중립**(huge recall ≥ ungated−0.05) —
  효과는 빌드 시간 한정. `part_count` 배선 3곳을 `(min>0 || max>0)`로 확장(누락 시 silent no-op).
- 신규 테스트 `tier2_payload_maxgate.sql`(skewed 픽스처, bucket 0 ~80%): filter/k 정확성, huge recall ≥
  ungated−0.05, 소형 파티션 top-10 불변, visit_cap=200 + 4워커 병렬 빌드 완주 + recall parity ±0.05.
- 권장 운영값: max_card ≈ N의 5-10%(10M→~500K-1M), visit_cap ≈ 4000.

### N1 — 배치 fetch-add 예약 (B1 글로벌 atomic 교체) (`4d5a461`)
발견 1의 캐시라인 바운싱 회귀 제거. per-node 글로벌 fetch-add 2개(`n_nodes`/`arena_used`)를
**워커별 배치 예약**으로: 워커가 256 id + arena 바이트 배치를 각 1회 fetch-add로 예약 후 로컬 lock-free
할당, 소진 시 재예약. atomic 트래픽 ~256배↓.
- **HOLE 처리(핵심 정확성):** 배치가 카운터를 실기록 수 너머로 전진 → 마지막 배치 미충전 슬롯이 구멍.
  `begin_parallel`이 전 노드를 `ACORN_NODE_DEAD`(level=-1)로 스탬프, 성공 push가 덮음, spill(-1)은 유지.
  모든 `0..n_nodes` 순회 사이트(build_payload_node / work-stealing pass-2 / leader pass / preassign Pass1·2 /
  flush Pass1·2·3)가 flush와 **동일 순서로** DEAD 스킵 → `Assert(blkno==nbr_blkno)` tripwire 유지.
- 직렬 경로(shared==NULL) 무변; spill(-1) 의미 보존(부분 배치는 버려져 DEAD 구멍 → flush 스킵).
- 신규 단언 `stress_no_phantom_tuples`(4워커/1MB-arena stress 빌드 후 `reltuples == count(*)`): DEAD 구멍이
  phantom 튜플로 새면 실패. 기존 spill/stress/entry recall parity가 mid-spill hole 손상 1차 가드.

### VM 실측 결과 (2026-06-16, 10M `tv_items`, m=16/efc=64/gamma=2/payload_m=64/non-inline/mwm=64GB/workers=30)

`tv_items` 분포는 **균일** — 100 bucket × 각 ~100K행(min 61820, max 152929). 1차 진단의 "거대 1M 파티션"은
이 픽스처에 **존재하지 않음** → N2 max_card 게이트는 여기서 no-op. 측정으로 1·2차 가정이 줄줄이 틀렸음이 드러남.

| 빌드 | wall-time | 판정 |
|------|-----------|------|
| OLD (baseline, f0a3308) | 87분 | 기준 |
| B1+B2 (글로벌 atomic) | 98분 | 회귀 |
| **N1 (배치예약, 인터리브)** | **115분** | **회귀 악화 → revert(50b3915)** |
| **N4 (two-pass + broadcast + visit_cap=4000)** | **88분** | **≈ OLD (빌드시간 개선 없음)** |

N4 분해: pass-1(기본그래프, 병렬) 19.5분 + pass-2(payload, 병렬) ~52분 + 직렬 flush/WAL-log(`log_newpage_range`) ~16분.

### 발견 3 (핵심) — pass-2 "직렬 꼬리"의 진짜 원인은 CV 배리어 버그 (N4가 수정)
1차의 "pass-2 직렬 꼬리"는 거대 파티션도, 무계 backfill도 아니었다. **라이브 gdb**로 확정:
- 워커의 `visit_cap`은 4000으로 **정상 전파**(GUC 문제 아님), search_partition도 유계로 빠름.
- 그런데 `pass2_next`가 13분에 488K/10M(4.9%), `pass2done=0`, **워커 28개가 `ParallelCreateIndexScan` 배리어에서 잠듦, 2~3개만 실행**.
- 원인: pass-1→pass-2 진입 배리어가 `ConditionVariableSignal`(하나만 깨움)을 사용. 31개 참가자가 같은 CV에서
  자는데 마지막 워커 신호가 **딱 1개만 깨움** → 나머지 ~28개 영원히 잠듦 → pass-2가 2~3코어로 직렬화.
  (무계 루프엔 `CHECK_FOR_INTERRUPTS`도 없어 빌드가 취소 불가로 멈추기까지 함.)
- **N4 수정**: 두 `workersdonecv` 신호를 `ConditionVariableBroadcast`로 → 전 워커가 깨어 조건 재확인 → 30워커
  전부 pass-2 진입. 측정 후 pass-2 idle 94%/2코어 → **4%/31코어**로 정상화. **B3 이후 모든 측정이 이 버그에 오염됨.**

### 결론 — two-pass는 빌드타임 레버가 아니다
broadcast로 pass-2를 제대로 병렬화해도 총합(88분) ≈ OLD 인터리브(87분). 총 작업량(기본+payload+flush)은
두 방식이 같고, 이제 병목은 **(a) payload 엣지 구축의 본연 연산량(~52분, 노드당 ~9ms)** + **(b) 82GB 인덱스의
직렬 WAL-log flush(~16분, 모든 빌드 공통)**. 이번 세션의 실제 가치는 **빌드시간 단축이 아니라 N4 동시성 버그
수정**(정확성·강건성)과 N2/N3 방어 상한(default-off).

### 후속 과제 (진짜 레버 후보)
- **flush/WAL-log 병렬화 or `wal_level=minimal` 경로**: ~16분 직렬 단계. 인터리브·two-pass 공통이라 가장 광범위.
- **payload 엣지 비용 절감**: 노드당 ~9ms(파티션 탐색 + ≤64 역방향 엣지 diversify). diversify/역방향 비용 분석.
- **B1/B2 atomic allocator/entry 회귀(98분 vs OLD 87)**: N1 revert는 B1 상태로만 복귀. 완전 제거하려면 B1/B2도
  revert 검토(단 B3 two-pass 인프라가 그 위에 빌드됨 — 의존성 확인 필요).
- pass-2 work-stealing granularity=1은 broadcast 수정 후엔 문제 아님(노드당 유계 비용, 꼬리 무시 가능).
