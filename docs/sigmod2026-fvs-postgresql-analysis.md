# SIGMOD 2026 FVS in PostgreSQL — 논문 분석 및 pg_acorn 설계 지침

**논문**: "An In-Depth Study of Filter-Agnostic Vector Search on a PostgreSQL Database System: [Experiments & Analysis]"  
**출처**: arXiv:2603.23710 · Proc. ACM Manag. Data 4(3), Article 134 (SIGMOD 2026)  
**저자**: Duo Lu (Brown), Helena Caminal, Yannis Papakonstantinou, Yannis Chronis, Vaibhav Jain, Fatma Özcan (Google), Manos Chatzakis (Université Paris Cité)  
**작성 목적**: pg_acorn 구현 결정에 직접 영향을 미치는 발견을 정리하고 설계 지침을 도출한다.

---

## 1. 논문의 핵심 주장

기존 FVS(Filtered Vector Search) 연구는 대부분 **standalone library**(HNSWLib 등)에서 벤치마크를 수행하며, PostgreSQL 같은 실제 DBMS의 시스템 오버헤드를 무시한다. 이 논문은 production-grade PG 호환 시스템(AlloyDB 기반)에서 filter-agnostic 알고리즘들을 실측하여, library 결과가 DBMS 결과와 **질적으로 다르다**는 것을 최초로 체계적으로 증명한다.

**Figure 1 요약** (OpenAI-1M, 100 쿼리 평균 레이턴시):

```
Selectivity     1%      10%     100%
Library gap:    2x      1.5x    1x     (ACORN vs Sweeping, library 유리)
PGVector gap:   역전    1.2x    1x     (교차점이 달라짐)
System cost:    최대 10x 높음
```

- PGVector 전체 레이턴시는 HNSWLib 대비 **최대 10x 높다**.
- 알고리즘 간 성능 교차점(crossover)이 library ↔ DBMS에서 **완전히 달라진다**.
- "distance computation 최소화"는 PG에서 end-to-end 성능의 신뢰할 수 없는 proxy다.

---

## 2. 왜 달라지는가 — 구조적 차이 (Table 1)

| 아키텍처 측면 | Library | PostgreSQL |
|-------------|---------|------------|
| **인덱스 저장** | raw pointer, 연속 메모리 | 8KB pages, 6-byte TID per neighbor |
| **노드 접근** | CPU 메모리 역참조 1회 | buffer pool lookup + page lock + 데이터 복사 |
| **필터 평가** | 단일 ID로 벡터/속성 동시 접근 | indextid → heap page → filter column (이중 조회) |
| **병렬성** | OpenMP, SIMD 자유 | 커넥션 당 1 쿼리, executor 제약 |

핵심: PG에서 **필터를 평가하려면 반드시 heap page를 추가로 읽어야 한다.** indextid(인덱스 내 포인터)가 heaptid(heap row 포인터)와 분리되어 있기 때문이다. ACORN의 2-hop 탐색은 이 비용을 기하급수적으로 증폭시킨다.

---

## 3. indextid와 heaptid — 이중 조회 문제

pgvector의 인덱스 페이지에는 두 종류의 포인터가 공존한다:

- **indextid** (6 bytes): 인덱스 내 다른 페이지를 가리키는 포인터 (BlockNumber + OffsetNumber). 이웃 노드의 인덱스상 위치.
- **heaptid** (6 bytes): 실제 테이블 row를 가리키는 포인터. 필터 컬럼(WHERE 조건)을 평가하려면 이것이 필요.

Library에서는 단일 ID로 벡터 데이터와 속성 데이터 모두 접근할 수 있다. PG에서는 반드시 두 단계를 거쳐야 한다:

```
(1) indextid → index page 접근 → heaptid 획득
(2) heaptid  → heap page 접근  → filter column 읽기 → 필터 평가
```

이것이 논문이 말하는 "double lookup"이다.

---

## 4. 2-hop 비용의 기하급수적 증가

ACORN/NaviX의 2-hop expansion이 PG에서 최악의 경우 유발하는 페이지 접근:

```
1 (현재 노드 index page)
+ M (1-hop 이웃 M개의 index page — 각각 indextid + heaptid 수집)
+ M × M (2-hop 이웃 M² 개의 index page — heaptid 수집)
= 1 + M + M²
```

M=32 기준: **1 + 32 + 1024 = 1057 page accesses per traversal step**.  
각 page access는 buffer pool lookup + shared lock acquisition + data read + lock release를 포함한다.

Library에서 2-hop expansion은 포인터 역참조 M²회다. 이 차이가 성능 교차점을 바꾼다.

---

## 5. Translation Map (TM) — 필수 최적화

### 정의

인덱스 **빌드 시점**에 전체 인덱스를 한 번 스캔해서 `indextid → heaptid` 인메모리 해시맵을 생성한다. 쿼리 시 2-hop 이웃의 heaptid가 필요하면 page access 없이 메모리 조회 1회로 해결.

```
2-hop indextid
  → TM.lookup(indextid) → heaptid  (page access 없음, ~10ns)
  → 필터 평가
```

TM 없이:
```
2-hop indextid
  → index page read (buffer lock + read, ~1-10μs)
  → heaptid 추출
  → 필터 평가
```

### 효과 (Figure 13, OpenAI-5M, no correlation)

| | TM 없음 | TM 있음 |
|--|--------|--------|
| heaptid 조회 비용 | **CPU 사이클의 60-75%** | 8-17% (TM overhead) |
| 나머지 병목 | heaptid fetch가 모두 잠식 | distcomp + filter check + metadata |

TM이 없으면 다른 최적화들이 의미 없어진다. heaptid fetch가 모든 CPU 사이클을 잠식하기 때문이다.

### 메모리 footprint

엔트리당 약 18 bytes (6 indextid + 6 heaptid + 6 overhead).
- 1M 벡터: ~18MB
- 35M 벡터: ~630MB
- GCP VM 128GB 환경에서는 무시 가능한 수준

### 논문의 위상

TM은 논문이 "We introduced"라고 표현한 **연구 프로토타입 최적화**다. 오픈소스 pgvector에도, 상용 AlloyDB 프로덕션에도 없다. Google 연구자들이 이 논문을 위해 구현한 것이다.

---

## 6. 논문이 평가한 알고리즘들

### 6.1 Filter-First (NaviX, ACORN)

저선택도에서 유리. 필터 통과 노드만 향해 그래프를 "tunneling"한다.

**ACORN (Blind)**: 1-hop 이웃 먼저, 그 다음 2-hop 임의 순서 탐색. 연결성 유지를 위해 모든 탐색 방향 동등하게 처리.

**NaviX**: ACORN-1 기반, 세 가지 휴리스틱을 매 탐색 스텝마다 **동적으로 전환**:

| 휴리스틱 | 동작 | 특징 |
|---------|------|------|
| Blind | 1-hop 먼저, 2-hop 임의 순서 | ACORN과 동일 |
| **Directed** | 1-hop을 거리 기준 정렬 후 가까운 것의 2-hop 우선 탐색 | ACORN 대비 1.2-1.7x 빠름 |
| Onehop-s | 1-hop 필터 통과 노드만, 2-hop 없음 | 선택도 높을 때 최적 |

NaviX의 adaptive-local 메커니즘: 현재 탐색 상태(C/W 채움률, 필터 통과율)를 보고 셋 중 하나를 선택.

**NaviX vs ACORN**: Directed 휴리스틱이 핵심이다. "더 유망한 1-hop의 2-hop을 먼저 탐색"한다는 한 가지 변경으로 불필요한 page access를 줄인다.

### 6.2 Traversal-First (Sweeping, Iterative Scan)

고선택도에서 유리. 필터를 무시하고 그래프 구조를 따라 탐색, 결과 큐에 넣기 직전에만 필터 체크.

**Sweeping**: 원본 HNSW 구조 그대로 탐색, W 진입 직전에 필터 평가.

**Iterative Scan**: pgvector 0.8.0의 resumable post-filter. 첫 라운드에 k개 미만이면 이전 상태(discarded queue D)에서 재개. 현재 오픈소스 pgvector에 포함된 유일한 filter-aware 기능.

### 6.3 ScaNN (Clustering-based)

Google의 클러스터 기반 인덱스. AlloyDB native extension으로 존재 (whitepaper [10]).

- Leaf 레벨에서만 필터링: root → branch → leaf까지는 unfiltered, leaf 내부에서만 heaptid 확인 후 filter-passing 벡터만 scoring.
- 순차 접근 패턴 + SIMD 친화적 → cache 효율 높음.
- 저차원(sift, text2image)에서 2-3x 우위. 고차원(openai, cohere)에서 격차 좁아짐.
- **음의 상관관계**에서 유일한 승자: 그래프 proximity에 의존하지 않아 negative correlation penalty 없음.

---

## 7. AlloyDB 상용 vs 연구 프로토타입 — 중요한 구분

논문은 "commercially available PostgreSQL-compatible system using the PGVector extension"에서 실험했다. 이 시스템은 AlloyDB다. 그러나 실험에 쓰인 구성 요소들의 상태가 다르다:

| 구성 요소 | 상태 | 어디서 사용 가능 |
|---------|------|---------------|
| Iterative Scan | 오픈소스 pgvector 0.8.0 | 누구나 |
| ScaNN | AlloyDB 상용 native 확장 | AlloyDB만 |
| **NaviX + TM + Adaptive 2-hop** | **연구 프로토타입 (이 논문용)** | **어디에도 없음** |
| **ACORN + TM + Adaptive 2-hop** | **연구 프로토타입 (이 논문용)** | **어디에도 없음** |
| Sweeping | **연구 프로토타입 (이 논문용)** | **어디에도 없음** |

논문 저자들(Google 연구자)이 이 연구를 위해 AlloyDB 위의 pgvector에 직접 패치해서 구현한 것이다. AlloyDB를 구독해도 NaviX나 TM+ACORN을 쓸 수 없다.

**결론**: pg_acorn이 TM + NaviX-Directed + ACORN을 구현하면, 오픈소스 PostgreSQL에서 제대로 된 graph-based filtered vector search를 제공하는 최초의 구현체가 된다.

---

## 8. 알고리즘별 성능 특성 (PG 환경)

### 8.1 선택도별 순위

| 선택도 | 1위 | 2위 | 하위 |
|--------|-----|-----|------|
| 1-5% | NaviX-Directed | ACORN | Sweeping, Iterative Scan |
| 5-30% | NaviX-Directed | ScaNN ≈ ACORN | Sweeping > Iterative Scan |
| 50%+ | ScaNN | Sweeping ≈ Iterative Scan | NaviX > ACORN |

### 8.2 상관관계별 특성

| 상관관계 | 승자 | 특이점 |
|---------|------|--------|
| High Positive | NaviX-Directed | Sweeping/Iterative Scan도 10% 이상에서 경쟁력 |
| Low Positive | NaviX-Directed | ScaNN이 저선택도 강하나 고선택도 약화 |
| **Negative** | **ScaNN 압도적** | NaviX -53%, ACORN -44%, Iterative Scan -89% 하락 |

### 8.3 k 변화에 대한 강건성 (k=5 → k=100)

| 방법 | hop/leaf 증가율 |
|------|--------------|
| NaviX | +86-106% |
| Sweeping | **+536-599%** |
| ScaNN | +220% |

대용량 k 쿼리에서 NaviX/ACORN이 가장 강건하다.

---

## 9. 시스템 오버헤드의 근원 — 병렬성과 무관

단일 스레드에서도 시스템 오버헤드가 압도적이다 (Table 7, OpenAI-5M, 10% selectivity):

| 방법 | System OH% (1T) | System OH% (16T) | 증가분 |
|------|----------------|-----------------|--------|
| NaviX | 55.9% | 73.5% | +48% |
| Sweeping | 81.8% | 91.0% | +68% |
| ScaNN | 84.4% | 86.9% | +59% |

단일 스레드 기준에서도 과반 이상이 시스템 오버헤드다. concurrency contention 문제가 아니라 **row-based page architecture의 구조적 비용**이다.

### ScaNN이 더 많은 filter check와 page access에도 CPU 사이클이 적은 이유

1. **순차 접근**: cluster leaf 내 벡터를 연속 페이지로 순차 스캔 → hardware prefetcher 활용
2. **SIMD 친화적**: 배치 bitmap probing + 거리 계산이 uniform access pattern으로 벡터화 가능
3. **낮은 system tax**: HNSW의 TM 조회(8-17M cycles), neighbor metadata retrieval(15-25M cycles) 같은 고정 비용 없음
4. **캐시 locality**: 조밀한 클러스터 내 벡터들이 cache에 함께 존재

---

## 10. ACORN-gamma의 pgvector 페이지 제약

pgvector의 neighbor tuple이 단일 8KB 페이지에 들어가야 한다:

```
(L_max + 2) × M × sizeof(ItemPointerData) ≤ 8192
sizeof(ItemPointerData) = 6 bytes
```

| M (= m × gamma) | L_max | 비고 |
|-----------------|-------|------|
| 16 | 63 | pgvector 기본값 |
| 32 | 31 | 논문 실험 설정 |
| 64 | 15 | 실용 한계 |
| 80 (gamma=2, m=40) | **12** | L_max 절반으로 붕괴 |
| 128 (gamma=4, m=32) | **6** | 사실상 flat graph |

gamma=2로 M을 두 배 늘리면 계층 깊이가 절반이 된다. `acorn_build.c`에서 이 trade-off를 계산하고 경고해야 한다.

---

## 11. 현재 pg_acorn 구현과의 격차

### 일치하는 부분

- **C/W 불변식**: 필터 실패 → C 유지, 필터 통과 → W. `acorn_layer0_search()` 471-477행에서 정확히 구현.
- **fixed-slot retry**: `acorn_add_reverse_edge()`에서 구현.
- **m_eff = m × gamma**: `acorn_m_eff()`에서 구현.

### 격차

| 항목 | 논문 권장 | 현재 구현 | 우선순위 |
|------|---------|----------|--------|
| **Translation Map** | 2-hop heaptid 조회 필수 최적화, 60-75% 절감 | 미구현 — heap fetch 직접 수행 | 높음 |
| **2-hop 확장** | ACORN-1 연결성 보장 | 미구현 — 1-hop만 탐색 | 중간 |
| **NaviX-Directed** | 1-hop 정렬 → 1.2-1.7x 향상 | 미구현 | 중간 |
| **Adaptive 2-hop skip** | 1-hop 통과 시 2-hop 건너뜀 | 미구현 (2-hop 없으므로) | 낮음 |
| **Multi-layer HNSW** | O(log N) greedy descent | flat graph (level=0) | 높음 |
| **ef_search C 상한** | 탐색 범위 제한 | 미구현, 코드에 TODO 있음 | 낮음 |

**중요**: 현재 flat graph(all level=0)는 Cohere 35M 규모에서 탐색 품질이 무너진다. multi-layer HNSW 구현이 Step 3의 핵심 남은 작업이다. TM과 2-hop은 그 이후 Step 6에서 NaviX-Directed(rank 2)와 묶어 구현한다.

---

## 12. pg_acorn 구현 지침

### 12.1 Translation Map — Step 6 NaviX-Directed와 함께 구현

```c
/* 빌드 시: acorn_build.c — 인덱스 완성 후 TM 생성 */
typedef struct AcornTranslationMap {
    HTAB *indextid_to_heaptid;  /* key: ItemPointerData (6B), val: ItemPointerData (6B) */
    MemoryContext mcxt;
} AcornTranslationMap;

/* 쿼리 시: acorn_scan.c — 2-hop heaptid 조회 */
ItemPointerData heaptid = acorn_tm_lookup(scan->tm, indextid);
bool passes = eval_predicate_by_heaptid(heaptid, scan->qual);
```

### 12.2 NaviX-Directed 2-hop 확장 — acorn_scan.c inner loop

```c
/* 1-hop 이웃을 거리 기준으로 정렬 */
qsort(neighbors_1hop, n_1hop, sizeof(AcornElem), cmp_by_distance);

foreach 1-hop neighbor v (정렬된 순서):
    if eval_predicate(v):
        add_to_W(v)
        /* Hardening ACORN: 이 브랜치 2-hop skip */
    else:
        add_to_C(v)  /* 연결성 유지 */
        foreach 2-hop neighbor u of v:
            heaptid = tm_lookup(u.indextid)  /* TM으로 page access 없이 */
            if eval_predicate_heaptid(heaptid):
                add_to_W(u)
            else:
                add_to_C(u)
```

### 12.3 ACORN-gamma L_max 경고 — acorn_build.c

```c
int m_eff = opts->m * opts->acorn_gamma;
int l_max = (int)(8192.0 / ((double)m_eff * sizeof(ItemPointerData))) - 2;

if (l_max < 10)
    ereport(WARNING,
        errmsg("acorn_gamma=%d with m=%d produces L_max=%d (< 10). "
               "Consider reducing gamma or m.",
               opts->acorn_gamma, opts->m, l_max));
```

### 12.4 Benchmark — page I/O 측정 필수

```python
# bench/harness/cohere_bench.py
def measure_page_io(conn, query_sql):
    rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query_sql).fetchall()
    plan = rows[0][0][0]
    shared_hit  = plan["Planning"]["Shared Hit Blocks"]  + plan["Execution"]["Shared Hit Blocks"]
    shared_read = plan["Planning"]["Shared Read Blocks"] + plan["Execution"]["Shared Read Blocks"]
    return {"pages_hit": shared_hit, "pages_read": shared_read}
```

벤치마크 리포트에 `QPS`, `recall@10`, `p99 latency`와 함께 **`pages_per_query`** 필수.

---

## 13. 장기 방향 — 컬럼나 엔진

논문의 결론이 명시적으로 제안:

> "A promising approach is the use of columnar engines to host both the index as well as the columns that participate in filtered vector search. Columnar engines have shown their ability to drastically reduce the page access overhead and other system overheads while maintaining transactional consistency."

row-based PG의 double-lookup을 근본적으로 해결하려면 필터 컬럼을 인덱스 페이지와 동일한 컬럼나 청크에 함께 저장해야 한다.

pg_acorn의 현재 CustomScan(Tier 1) 접근은 heap access를 최소화하는 방향에서 이 개념과 정렬한다. 더 나아가 `acorn_hnsw` 인덱스 내에 filter column 사본을 저장하는 "included columns" 방식(PG 11+의 `INCLUDE` 절)을 장기적으로 검토할 수 있다.

---

## 참고

- 논문 원문: https://arxiv.org/abs/2603.23710
- ScaNN for AlloyDB whitepaper: https://services.google.com/fh/files/misc/scann_for_alloydb_whitepaper.pdf
- pgvector 0.8.0 Iterative Scan: https://github.com/pgvector/pgvector
- RF-ANNS 서베이 (분류 체계): arXiv:2505.06501
- pg_acorn 아키텍처: `docs/architecture.md`
- 산업 지형: `docs/filterable-hnsw-landscape.md`
- Post-ACORN 실험 계획: `~/.claude/plans/filterable-jazzy-dongarra.md` Step 6
