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
