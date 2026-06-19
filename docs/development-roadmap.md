# pg_acorn 발전 로드맵

> 최초 작성: 2026-06-08 (SIGMOD 2026 FVS-in-PG 분석 기반, 쿼리-side Phase 0→3)
> 현 시점 재정리: 2026-06-19 — 3-way 벤치·스케일링, 빌드-perf 캠페인(B1-B4/N1-N4),
> M-ACORN 음성 결론, extension-lock 라이브락 발견을 반영해 **트랙 구조로 재편**.
> 2026-06-19 (2차): **그랜드 플랜(North Star + 4-Phase) + Track S(안정화: 대용량·빌드병렬·
> 다중세션, 1.0 게이트)** 추가.
> 근거 문서: `docs/project-log.md`(마스터 원장), `bench/COMPETITIVE_VERDICT.md`(경쟁판정 SSOT),
> `docs/sigmod2026-fvs-postgresql-analysis.md`, `docs/macorn-penalty-findings.md`,
> `docs/build-perf-backlog.md`, `bench/OVERHEAD_LEDGER.md`.

---

## 0. 갱신된 baseline — 무엇이 끝났나

| 영역 | 상태 |
|------|------|
| 멀티레이어 HNSW 빌드 / ACORN-γ / 공유 탐색 (Tier 1 hook + Tier 2 `acorn_hnsw` AM) | 완료 |
| **Phase 0 (측정·안전)**: page-I/O 계측, L_max(gamma-clamp) 경고, resume 스트리밍 스캔, 비용모델 재보정 | **완료** |
| 실데이터 + Qdrant + 3-way 스케일링 (100K/1M/10M) | **완료** (`REPORT_scale`/`REPORT_3way`) |
| Qdrant 차용 5종 (payload_m, auto-ef, payload-gate, telemetry, plan-choice) | 완료 |
| code-cache 스캔 최적화 | 완료 (default ON) |
| 빌드-perf 캠페인 (B1-B4, N1-N4) | 완료 — N4 CV-broadcast 버그 수정, two-pass/N1 음성 결론 |
| M-ACORN (빌드-시 predicate penalty) | **음성 결론** — ACORN scan-time gamma와 중복, 휴면 (`feat/macorn-penalty`) |

원래 로드맵의 **Phase 0는 사실상 닫혔다** (유일 잔여: `ef_search` C 상한 — 저우선 보류). 미해결 잔여:
`ef_search` C 상한(`acorn_scan.c:515` TODO, 저우선).

### 측정으로 확정된 격차/한계 (이 로드맵의 구동력)

| 한계 | 근거 | 영향 트랙 |
|------|------|----------|
| 고선택도+matched-recall에서 Qdrant latency 갭 (**배수 미해결/INDICATIVE**) | `REPORT_qdrant_final`(250K, cross-substrate)·`COMPETITIVE_VERDICT` | C |
| 대용량 빌드 **메모리 벽**: 그래프가 mwm 초과→on-disk 스필 | 2M@8GB·10M@32GB 실측 | B |
| 병렬 빌드 **extension-lock 라이브락** (스필+다워커) | 10M/8워커 분당 2블록 정체 | B |
| atomic allocator 회귀 (병렬 98 vs OLD 87분) | 빌드-perf 캠페인 | B |
| 직렬 빌드 꼬리 (WAL flush ~16분/82GB + P_NEW 한장씩) | 캠페인 + 라이브락 진단 | B |
| 쿼리 동시처리 **미검증** (throughput INDICATIVE) | 본 세션 | E |
| 단일 노드 / int4·256-파티션 필터 모델 | 코드 구조 | D |

---

## 그랜드 플랜 — North Star + 4-Phase 아크

**North Star:** *PostgreSQL 안에서 전용 벡터엔진(Qdrant/Pinecone)에 필적하는 필터드 ANN* —
트랜잭션·SQL·조인·on-disk를 유지한 채. pgvector 초월은 이미 달성. 다음은 전용엔진과의
**substrate(버퍼매니저) 갭**을 좁히는 것. 핵심 통찰(측정): 남은 갭은 거리계산이 아니라
**per-neighbor 버퍼 비용** — 스캔 exec의 ~45%가 neighbor 이중로드(`OVERHEAD_LEDGER.md`).

| Phase | 버전 | 목표 | 핵심 작업 | 졸업 기준 |
|-------|------|------|-----------|-----------|
| **I. 정직한 베이스라인 + quick win** | 0.1.x | 경쟁 숫자 확정 + 측정된 저위험 이득 | C0 재측정, **C1 이중로드 dedup(~1.5x)**, B1 벌크 사전확장, B3 atomic 게이트 | Qdrant 갭 단일 숫자 확정 + C1 실측 + 10M 병렬 빌드 완주 |
| **II. substrate 갭 종결** (승부처) | 0.2.x | 버퍼 per-hop 비용 구조 제거 | shmem topology-colocated graph(~2.6x, ledger #4), D2 INCLUDE 컬럼나 | 고선택도서도 Qdrant 동급 latency |
| **III. 스케일 & 폭** | 0.3.x+ | 대용량·필터 폭 | B2 flush 병렬화, mwm/spill 견고화, D1 멀티/범위 필터, upstream PR | 10M+ 깨끗한 빌드/쿼리, 새 질의클래스 |
| **IV. 안정화 & 1.0** | 1.0 | 견고성·안정성 약속 | **Track S 전체(아래)**, API/포맷 안정화 | Track S exit 충족 + 논제 실증 |

> **투영치(~1.5x, ~2.6x)는 ledger(n=30K) 근거의 예측**이며 Phase I C0가 스케일에서 확정한다.

```
0.1.0 (now) ── 이김:pgvector / parity:recall / 갭:substrate-buffer
   │  Phase I  0.1.x ── C0 재측정 + C1 이중로드 dedup + B1 라이브락 제거
   │  Phase II 0.2.x ── colocated graph + INCLUDE → 전용엔진 필적 [승부처]
   │  Phase III 0.3.x ── 10M+ 빌드 / 범위·멀티필터 / upstream
   └  Phase IV 1.0  ── Track S(안정화) 완수 + 논제 실증
```

**관통 규율(이번 정리의 제도화):** ① 측정 우선(레버는 page-I/O로 증명 후 주장) ② 경쟁 숫자는
`COMPETITIVE_VERDICT.md` 단일 진실원, 단일 배수 quote 금지 ③ **반증 가설 재시도 금지**(two-pass·
M-ACORN·2-hop·N1 — `project-log.md` disproven) ④ 매 항해 후 `project-log.md`+memory `project-map` 갱신.

---

## 개발 축 (트랙) — 메인(B/C/D) + 안정화(S, 1.0 게이트) + 강등된 A

### Track A — 쿼리 알고리즘: 2-hop은 시도·폐기됨 (강등)

> **정정(2026-06-19)**: 원 로드맵(06-08)은 TM→2-hop→NaviX를 "연구 간판"으로 베팅했으나,
> 06-10에 **실제 구현·벤치된 뒤 폐기**됐다. 이 트랙은 미래작업이 아니라 *측정으로 반증된 가설*이다.

- **A1. Translation Map — 부분 실현됨**: TM의 핵심 이득(필터 평가 시 heaptid 재조회 제거)은
  `feb2d7c`의 query-local 노드 캐시(`acorn_t2_load_node` — element 페이지 단일 읽기로
  distance+metadata 결합, `acorn_scan.c`)로 **이미 실현돼 main에 살아있다**. 독립 HTAB TM은
  2-hop이 없으면 추가 실익이 불분명 → 보류.
- **A2/A3. 런타임 2-hop (ACORN-1) + NaviX-Directed — 구현 후 폐기 (근거 있음)**:
  `feb2d7c`가 NaviX-Directed 2-hop을 TDD로 구현(filter-실패 1-hop 노드를 거리순 min-heap D로
  모아 k_d=Max(m/2,4)개 확장, C 소진 시 drain). 벤치(`bench/RESULTS.md`)에서 **스케일 회귀로 패배**:
  - n=50K선 +0.017(도움) → **n=200K선 역전**: tier2_g1 @10% 선택도 2hop-**on 0.779 vs off 0.890 = −0.111**
  - "tier2_g2 (2hop off)가 이 스케일 최고 변종". 원인: D-injection이 가까운 *통과* 노드를 더 먼
    2-hop 이웃으로 밀어냄 → "보수적 drain 정책 필요"(RESULTS.md)
  - `283e10b`: 벤치 스윕에서 **2-hop 변종을 빼고 gamma=4로 교체** → **build-time ACORN-γ가 승자**
  - **0.1.0 정리**: 스캔 코드는 이미 삭제돼 있었고 `enable_2hop` GUC + `tier2_2hop` 테스트만
    inert 잔재로 남아 **제거**함. Phase 1 노드캐시는 유지.
- **남은 투기적 여지 (저신뢰)**: 보수적 D-drain 정책으로 2-hop 부활이 *가능할 수도* 있으나,
  측정상 γ-확장이 더 단순·강력해 우선순위 낮음. 재도전 시 **≥200K에서 γ=4 대비 A/B**가 게이트.

### Track B — 빌드 확장성/성능 (대용량 enabler · 본 세션 신규 트랙)
*왜*: 대용량의 실질 한계가 빌드 메모리 벽 + 직렬/라이브락임을 측정으로 확인. 상세는
`docs/build-perf-backlog.md`, 라이브락은 프로젝트 메모리 `build-extension-lock-livelock`.

- **B1. 벌크 사전확장 (최우선)** — `ReadBufferExtended(P_NEW)` 한장씩(`acorn_build.c:304`) →
  HNSW가 아는 최종 노드/페이지 수로 `smgrzeroextend` 일괄 확장 후 사전할당 블록에 기록.
  **병렬 빌드 extension-lock 라이브락 제거** + 직렬 빌드도 가속. flush 코드 한정, 중난이도.
- **B2. flush 병렬화** — 직렬 `log_newpage_range` flush(~16분/82GB at 10M)를 워커 분산.
- **B3. atomic allocator 회귀 정리** — B1/B2 lock-free 할당의 캐시라인 바운싱(98 vs 87분) 완화
  또는 GUC 게이트로 OLD 경로 선택 가능하게.
- **B4. spill 경로 견고화** — mwm 초과 시 graceful degrade (현재는 병렬 라이브락/직렬 초저속).

### Track C — 경쟁 갭: 고선택도 latency (승부처)
*왜*: **recall은 Qdrant와 parity**(Z3 후 0.97-1.0). 남은 건 **고선택도+matched-recall의 latency
갭** — 단 **크기 미해결**(INDICATIVE, cross-substrate). 권위 판정은 `bench/COMPETITIVE_VERDICT.md`.
ledger(`OVERHEAD_LEDGER.md`)가 최대 수정가능 항목을 이미 짚음: `acorn_stream_expand`의
**neighbor 이중로드**(element 페이지를 distance·heaptid용으로 2회 핀) = scan exec의 ~45%.

- **C0. (전제) 깨끗한 재측정** — same-protocol·median-basis(prepared·literal·median+p99 분리),
  >=200K에서 Qdrant 갭 magnitude 확정. "1.6-4.4x"는 이게 끝나기 전엔 사실로 인용 금지.
- **C1. neighbor 이중로드 dedup** (ledger route #2) — element 페이지 1회 핀으로 distance+heaptid+
  deleted 동시 처리. 투영 ~1.5x(775→1170 QPS @ recall 0.953). C 변경, 저위험.
- **C2. 추가 처방** — 후보 C 상한(`ef_search` cap, 기존 TODO), 거리계산 SIMD/양자화,
  heap fetch 절감(→ Track D2 INCLUDE 연결). C0/C1 결과로 결정.

> 주: 한때 저선택도용으로 기대했던 2-hop은 폐기됐고(Track A), 고선택도 갭은 애초에 별도 문제다.

### Track D — 데이터모델/스코프 확장 (장기 · 큰 결정)
- **D1. 필터 모델 확장** — 현재 단일 int4 + 256 파티션(`filter_val & 255`) 한계. 멀티컬럼/
  고카디널리티/범위 필터. 범위로 가면 SeRF/KHI 영역 = 새 AM/질의클래스 결정.
- **D2. INCLUDE/컬럼나 필터 사본 (§13)** — 필터 컬럼 사본을 인덱스에 저장(PG11+ `INCLUDE`)해
  heap double-lookup 근본 제거. 논문 명시 장기 방향. 설계 스파이크부터.

### Track E — 검증/운영 (지속)
- **E1. 쿼리 동시처리 실측 (미검증)** — 멀티클라이언트 QPS 코어 스케일링, 락/공유버퍼 병목.
  코드상 동시 스캔 대응 흔적(overlapping scan ref, backend-local 카운터)은 있으나 부하 실측 없음.
- **E2. latency/throughput 정밀 측정** — 현재 INDICATIVE 단계 탈출(측정 환경 확정).
- **E3. index-fits-in-RAM 특성화**, 단일노드 천장 문서화.

> E1/E2/E3는 아래 Track S(안정화)의 *검증* 입력이다 — S가 그것을 하드닝 작업으로 흡수한다.

### Track S — Stabilization / Hardening (**1.0 게이트**)
*왜*: 성능 트랙(B/C/D)과 별개로, 견고성 3축을 명시적으로 안정화해야 1.0을 부를 수 있다.
기반은 있으나(BUG-3 동시성 수정 + isolation 4종) **부하·스케일 하 검증·하드닝이 비어 있다.**

- **S1. 대용량 안정화** — (a) index > shared_buffers 랜덤-IO 절벽 특성화 → PG17 prefetch/ReadStream
  검토(ledger #7: 콜드일 때만 유효), (b) 10M+ 크래시복구·재시작 무결성 + 대규모 빌드 RSS 회귀 가드,
  (c) spill 경로 graceful degrade(B1과 묶음). [E3 흡수]
- **S2. 빌드 병렬 안정화** — (a) B1 후 **spill+병렬 stress 테스트 신설**(현 parity는 in-memory 위주),
  (b) atomic 회귀 GUC 게이트(B3) + 워커 스케일링 상한 문서화, (c) build_seed 결정성 회귀 가드.
- **S3. 다중세션 안전성 (최우선 보강)** — (a) **E1 부하 검증**: pgbench 16/32 동접 필터드-KNN QPS
  코어 스케일링 + LWLock 경합 계측(pgvector #766 류), (b) code-cache **지속 churn + 고동접** stress
  로 "ef=1600 크래시=환경적" 가설을 결정적으로 닫기, (c) 동시 insert/update/delete + scan + vacuum
  race + MVCC 가시성 stress(현 `concurrent_insert_scan` 너머).
  *exit*: 정의된 동접·스케일에서 0 크래시 · 0 잘못된결과 · QPS 선형 스케일 입증.

> **자산**: 병렬빌드 parity/spill/stress 회귀(`tier2_build_parallel`), code-cache 동시성(BUG-3
> dsa double-unpin 수정 + hazard ptr) + isolation 4종(`concurrent_insert_scan`/`cache_insert_scan`/
> `cache_evict_scan`/`gamma_build`). S는 이 위에 **부하·스케일 차원**을 더한다.

---

## 의존성 그래프 / 시퀀스

```
[지금]
 ├─ C0 (깨끗한 재측정) → C1 (이중로드 dedup) → C2     ← 갭 magnitude 미해결, 저위험 진입
 ├─ B1 (벌크 사전확장) ──→ B2/B4                        ← 대용량 빌드 잠금 해제, 자족적
 └─ A1 노드캐시 실현됨 · A2/A3 2-hop은 폐기(γ 승)        ← 트랙 강등, 부활은 투기적
                                  │
[E1/E2 측정]은 C 효과 증명의 전제 ┘
```

**왜 이 순서인가**: (1) C·B는 *측정으로 확정된 한계*를 직접 공략하고 자족적이라 즉시 가치.
(2) Track A의 핵심(2-hop)은 이미 시도→폐기됐으므로 신규 알고리즘 작업은 우선순위에서 내려간다 —
γ-확장이 현 시점 답. (3) SIGMOD §1: "distance 계산 수는 PG end-to-end의 신뢰 못 할 proxy" →
어떤 신규 쿼리 최적화든 page-I/O(Phase 0 완료)로 **≥200K에서 γ=4 대비 A/B 증명**이 게이트.

## 우선순위 요약

| 순위 | 항목 | Phase | 근거 |
|------|------|-------|------|
| **P0** | C0 깨끗한 재측정 → C1 이중로드 dedup | I | 유일 잔여 성능 갭(magnitude 미해결), 진입 저위험, ~1.5x |
| **P0** | B1 벌크 사전확장 | I | 대용량 빌드 라이브락 제거 — 자족적, S1·S2 교집합 |
| **P1** | **S3 다중세션 안전성** (E1 부하검증부터) | I→IV | 가장 미검증·"기본기"의 핵심 — 프로덕션 신뢰 전제 |
| **P2** | S1 대용량 안정화 / S2 빌드병렬 안정화 / B2 flush | III·IV | B1 위에서 하드닝; 1.0 게이트 |
| **P2** | C2 추가 처방 (ef cap·SIMD·heap fetch) | II | C0/C1 결과로 결정 |
| **P3** | colocated graph(II) / D 데이터모델·컬럼나 | II·III | 승부처·큰 아키텍처 결정 |
| **보류** | A2 2-hop 부활(보수적 D-drain) | — | 이미 폐기됨(γ 승) — 투기적, 저신뢰 |

> **1.0 게이트 = Track S 전체 exit 충족.** 성능(C/colocated)이 1.0을 부르지 않는다 — 견고성이 부른다.

## 권장 다음 1~2 액션
1. **B1 (벌크 사전확장)** — 대용량 빌드 라이브락/저속을 정공법으로 제거. 자족적·즉시 가치, A/C와 독립.
2. **C0/C1 (Qdrant 갭)** — 깨끗한 median-basis 재측정으로 magnitude 확정 + ledger route #2(neighbor 이중로드 dedup, ~1.5x) 구현.

---

## 부록 — 신규 알고리즘 지형 (FAVOR / JAG / Curator / SeRF / KHI)

대부분 Track D(스코프 확장) 이후. 세 게이트로 *언제*가 갈린다.
- **G1 아키텍처 적합**: filter-agnostic이며 pgvector 페이지 포맷(`hnsw_compat.h`) 재사용 가능?
- **G2 문제 동일성**: 속성 *동등* WHERE 술어 문제? (범위/멀티테넌트는 다른 문제)
- **G3 PG 검증**: PG에서 우위 입증? (측정 *인프라*는 완비 — page-I/O 계측 + 실데이터 3-way
  하네스. 우리 ACORN-γ는 측정됨. 단 아래 신규 알고리즘들은 **PG 포팅 자체가 없어** PG 우위 미지.)

| 알고리즘 | 계열 | G1 | G2 | 배치 | 비고 |
|---------|------|----|----|------|------|
| **FAVOR** ([2605.07770](https://arxiv.org/abs/2605.07770)) | filter-agnostic graph (HNSW) | O | O | **투기적** (2-hop 폐기 후) | ACORN/NaviX의 사촌. selectivity-aware exclusion distance. 폐기된 2-hop의 "보수적 drain" 대안이 될 수 있으나 ≥200K에서 γ 대비 입증 필요 |
| **Curator** ([2401.07119](https://arxiv.org/abs/2401.07119)) | 파티션/트리 (per-label) | X | △ | Track D | ScaNN 계열. 저선택도·멀티테넌트·음상관 보완. 새 AM |
| **JAG** ([2602.10258](https://arxiv.org/abs/2602.10258)) | attribute graph | X | O | Track D+ | 속성을 그래프에 결합. 새 디스크 포맷/AM |
| **SeRF** ([SIGMOD'24](https://miaoqiao.github.io/paper/SIGMOD24_SeRF.pdf)) | range-filter segment graph | X | X | Track D+ | 범위 필터 전용. 질의 클래스 확장 결정 |
| **KHI** | 다속성 파티션 트리 + per-node HNSW | X | X | Track D+ | 다속성 범위 RFANNS. 새 AM |

- **부류 A (FAVOR)**: 우리 filter-agnostic graph 계열 → Track A에 가깝게 끼울 수 있음.
- **부류 B (JAG/Curator/SeRF/KHI)**: 필터를 인덱스에 박는 specialized index → 새 AM/다른 질의클래스.
  "채택"이 아니라 "방향 전환", Track D 이후. 전부 standalone 라이브러리 벤치 결과뿐 → 우리 하네스로
  측정하려면 **PG 포팅이 선행**돼야 함 (측정 인프라는 이미 있음).

> 서베이: RF-ANNS 분류 [arXiv:2505.06501](https://arxiv.org/abs/2505.06501),
> FANNS 벤치 [arXiv:2507.21989](https://arxiv.org/html/2507.21989v1).
