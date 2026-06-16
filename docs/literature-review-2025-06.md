# Literature Review: Filtered ANNS (June 2025)

두 논문을 검토하여 pg_acorn 프로젝트에 대한 시사점을 정리한다.

---

## 논문 1: FANNS Survey (arxiv 2505.06501)

**"Survey of Filtered Approximate Nearest Neighbor Search over the Vector-Scalar Hybrid Data"**
Yanjun Lin, Kai Zhang, Zhenying He, Yinan Jing, X. Sean Wang — 2025-05-10

### 배경 및 기여

기존의 pre/in/post-filter 3분류 대신 **pruning 전략 기반 4분류 taxonomy**를 제안한 서베이.
17개 알고리즘을 통일된 프레임워크에 매핑하고, 쿼리 난이도의 분포론적 정의를 제공한다.

### 핵심 프레임워크: VSP / VJP / SJP / SSP

| 전략 | 전체 이름 | 핵심 동작 | 적합 selectivity |
|------|-----------|-----------|-----------------|
| VSP | Vector-Solely Pruning | 벡터 거리만으로 후보 제거; 스칼라 조건 미적용 | 높음 (필터 느슨함) |
| VJP | Vector-centric Joint Pruning | 벡터 그래프 탐색 중 스칼라 조건 결합 | 중간~낮음 |
| SJP | Scalar-centric Joint Pruning | 스칼라 파티션 선행, 그 내부에서 벡터 탐색 | 낮음 (필터 타이트함) |
| SSP | Scalar-Solely Pruning | 스칼라 조건으로 완전 필터 후 벡터 검색 | 매우 낮음 |

### pg_acorn의 위치: VJP

논문은 ACORN을 VJP로 명시 분류한다. 현재 pg_acorn의 `acorn_scan.c` 구현도 정확히 이에 해당한다:

```c
/* acorn_scan.c:1085 */
/* ACORN invariant preserved: filter-failing nodes go to C only (connectivity);
 * filter-passing nodes go to both C and R (candidate + result). */
```

모든 노드를 C(탐색 후보)에 두고, 필터를 통과한 노드만 R(결과)에 넣는 구조가 VJP의 정의와 일치한다.

---

### VJP의 근본 한계: Disconnected Predicate Subgraph

논문은 VJP 알고리즘 전체의 공통 약점을 명확히 기술한다:

> "Uncertainty around the connectivity of the traversed subgraph makes search results
> potentially unreliable. A sparse graph often leads to a disconnected predicate subgraph,
> necessitating retention of the Dense Graph (DG)."

이는 프로젝트에서 경험한 현상들의 이론적 근거다:
- `acorn-search-not-build-gap`: "~0.85 recall ceiling is an ef-budget artifact"
- `scan-emission-order-bug`: sel=10/20% 천장이 buffered-emission 수정 후 해소 (0.990/0.960)
- 1M+ 스케일에서의 고선택도(낮은 selectivity) 구간 recall 한계

논문이 제시하는 해법은 M 파라미터를 높게 유지해 Dense Graph를 보장하는 것이다.
대규모(10M) 테스트 시 M 스윕이 필요한 이유가 여기에 있다.

---

### 주목할 알고리즘들

**AIRSHIP (VJP 변형)**

> "Probabilistically visits both filter-satisfying AND filter-violating data points during
> neighbor expansion, exploiting satisfied points for efficiency while exploring
> unsatisfied points for comprehensive coverage."

pg_acorn이 이미 이 원칙을 구현하고 있다 (filter-failing → C, filter-passing → C+R).
AIRSHIP과의 차이가 있다면 — 확률적 방문 비율의 명시적 제어 — pg_acorn에서 탐색 비용 절감을 위한
튜닝 파라미터로 추가 가능하다.

**StitchedVamana / FilteredVamana (SJP)**

> "Constructs separate graphs for each discrete scalar value, then overlays
> these scalar-specific subgraphs while selectively retaining edges."

pg_acorn의 `payload_gate` (파티션 cardinality 기반 게이팅)와 유사한 SJP 전략이다.
논문이 이를 독립 분류군으로 인정했다는 점이 payload_gate 아키텍처를 사후 정당화한다.
next-step으로는 파티션 값별 명시적 subgraph를 build 시 구성해 predicate subgraph
connectivity 문제 자체를 우회하는 방향을 검토할 수 있다.

**SeRF / iRangeGraph (range filter 전용 VJP)**

현재 pg_acorn은 equality 조건(`filter_val = ?`)만 지원한다.
SeRF는 range filter (`price BETWEEN 10 AND 50`)를 위해 Segment Graph 구조를 사용한다.
Qdrant가 range filter를 지원하는 상황에서 로드맵 우선순위 검토 가치가 있다.

**NHQ / HQANN (fusion vector)**

스칼라 속성을 numeric vector로 인코딩해 feature vector와 concatenate한 "fusion vector"를 만든다.
pg_acorn 아키텍처와 근본적으로 달라 단기 적용은 어려우나,
다중 속성 필터(AND/OR 조건) 지원 시 장기 참고 대상이다.

---

### 쿼리 난이도의 정량화: Wasserstein Distance

논문은 "query hardness"를 다음으로 정의한다:

> "Wasserstein distance between the vector distribution of the filtered subset
> and the full dataset."

검증은 UMAP 시각화(정성) + Wasserstein-2 거리(정량)로 이루어진다.
필터링된 서브셋의 벡터 분포가 전체 데이터셋과 크게 다를수록 쿼리가 어렵다 — 인덱스가
원래 분포에 최적화되어 있기 때문이다.

현재 벤치마크는 "correlated vs uncorrelated" 시나리오를 비공식적으로 구분해왔다 (g4 등).
벤치마크 스크립트에 Wasserstein-2 계산을 추가하면 "pg_acorn은 어려운 쿼리(W2 > θ)에서
X배 이득"과 같은 더 rigorous한 클레임이 가능해진다.

```python
# bench3way_report.py 또는 scale_report.py 추가 예시
from scipy.stats import wasserstein_distance   # 1D 근사
# 고차원의 경우: pip install POT (Python Optimal Transport)
import ot
W2 = ot.sliced_wasserstein_distance(full_vecs, filtered_vecs, n_projections=200)
```

---

### 논문의 Open Problems — 프로젝트 현황과 대조

| 논문의 미해결 문제 | pg_acorn 현황 |
|-------------------|--------------|
| 동적 알고리즘 전환 (selectivity 기반 자동 선택) | `acorn_cost.c` + planner hook 구현 중 |
| Realistic workload 최적화 | auto-ef histogram (P3) |
| 여러 알고리즘의 앙상블/조합 | Tier1/Tier2 분리가 partial 앙상블 역할 |

프로젝트가 field-wide open problem을 독립적으로 선행 탐색 중임을 확인한다.

---

---

## 논문 2: M-ACORN (연세대 DeLab)

**"M-ACORN: ACORN 기반의 메타데이터 통합 구축을 통한 Hybrid Query 최적화 알고리즘"**
양현서, 김휘군, 권세인, 이지은, 박상현† — 연세대학교 컴퓨터과학과

> 출처: https://delab.yonsei.ac.kr/assets/files/publication/domestic/conference/

### 핵심 아이디어

ACORN이 build 단계에서 metadata를 무시하는 약점을 직접 공략한다.
Build 시 **동일 파티션 이웃을 우선 선택**하도록 거리에 penalty를 더해,
같은 metadata를 가진 노드들이 자연스럽게 군집화된 그래프를 구축한다.

> "그래프 구축 과정에서 metadata가 다른 노드들은 penalty를 부여하여
> metadata가 서로 다른 노드들끼리 이웃이 될 확률을 낮추고,
> metadata가 같은 노드들끼리 군집화되도록 한다."

**검색 알고리즘은 변경 없다.** Build 단계만 수정한다.

---

### 알고리즘 상세

#### Penalty 계산 (빌드 전, 1회)

```
1. 데이터셋에서 n개 샘플 무작위 추출
2. 모든 조합의 L2 거리 집합 D 계산
3. 평균 거리 d_avg = mean(D)                 ← 데이터 스케일에 자동 정규화
4. penalty π = d_avg × penalty_factor         (pf 기본값=1.0; 최적 1.5~1.75)
```

평균 거리 기반이므로 차원 수나 데이터 스케일에 관계없이 자동으로 적절한 크기가 결정된다.

#### Build 시 searchNeighborsToAdd 수정 (Algorithm 1)

```
for each neighbor candidate u of current node v:
    d_u = distance(q, u)
    if metadata[u] != metadata[v]:   // 파티션 값이 다름
        d_u += π                     // penalty 부여
    // d_u가 작은 (= 같은 파티션) 노드를 이웃으로 우선 선택
```

결과: 같은 파티션 노드들이 이웃으로 선택될 확률이 높아지고,
predicate subgraph의 연결성(connectivity)이 빌드 단계에서부터 강화된다.

---

### 실험 결과 (SIFT1M, M=16, efc=40, metadata=random 1~12)

| 비교 대상 | Recall@95% QPS | Recall@97% QPS |
|-----------|---------------|---------------|
| **M-ACORN (pf=1.5)** | **3480** | **2150** |
| ACORN | 2486 | 1869 |
| 향상률 | **+40%** | **+15%** |
| post-filter (recall max ~78%) | 2340 | — |
| M-ACORN vs post-filter | **5~6배** | — |
| pre-filter (brute force) | QPS=16.91, recall=1.0 | — |

penalty factor별 trade-off: **1.5 → recall 최고 / 1.75 → QPS 최고**

---

### pg_acorn 적용 분석

#### 구현 위치: `acorn_build.c`

현재 pg_acorn은 build 시 pgvector의 HNSW 이웃 선택 로직을 그대로 사용하며,
`filter_val`을 이웃 선택 기준에 반영하지 않는다.
M-ACORN 수정은 이웃 거리 계산 직후 조건문 2줄이다:

```c
/* acorn_build.c — 이웃 후보 거리 계산 직후 삽입 */
if (candidate_filter_val != current_filter_val)
    d += acorn_build_penalty;     /* 새 GUC: pg_acorn.build_penalty_factor */
```

penalty 값은 빌드 전 샘플링으로 자동 계산하거나,
`pg_acorn.build_penalty_factor` GUC(기본값 1.5)를 통해 수동 조정 가능하게 한다.

#### payload_edges (B3)와의 관계

두 메커니즘은 상호 보완적으로 다른 레이어에서 작동한다:

| 메커니즘 | 작동 시점 | 효과 |
|----------|-----------|------|
| payload_edges (B3) | Build — 명시적 추가 | 필터 통과 노드 간 직접 연결 보장 |
| M-ACORN penalty | Build — 이웃 선택 편향 | base graph 전체의 soft clustering |

penalty로 base graph가 파티션별로 클러스터링되면,
payload_edges가 남은 단절 구간을 브릿지하는 구조다.
**조합 사용 시 각각보다 강한 predicate subgraph connectivity 기대.**

#### Scan 경로 무변경

M-ACORN은 검색 알고리즘을 변경하지 않는다. Tier1/Tier2 분리, SQ8 quantization,
code cache, buffered emission 등 현재의 모든 scan 최적화가 그대로 작동한다.
**적용 리스크가 매우 낮다.**

---

### 주의사항 및 한계

| 항목 | 논문 조건 | pg_acorn 확인 필요 |
|------|-----------|-------------------|
| 데이터셋 규모 | SIFT1M만 테스트 | 250K~10M 스케일 검증 필요 |
| Metadata 분포 | random 1~12 (균등, sel≈8.3%) | correlated 시나리오(g4 등) 효과 미확인 |
| 필터 속성 수 | 단일 정수 속성 | pg_acorn filter_val이 단일 int64이므로 직접 대응 |
| penalty_factor 최적값 | SIFT1M 기준 1.5~1.75 | 다른 데이터셋/차원에서 재조정 필요 |
| Build 시간 오버헤드 | 샘플링 비용 무시할 수준 | 이웃 비교 시 조건문 추가는 무시할 수준 |

---

### 검증 계획

```
1. acorn_build.c에 penalty 파라미터 추가 (pg_acorn.build_penalty_factor GUC, 기본 1.5)
2. build 전 샘플 기반 평균 거리 계산 → π 자동 설정
3. 250K 데이터셋으로 recall-QPS 곡선 재측정 (bench3way_pg.py 활용)
4. penalty_factor 0 / 1.0 / 1.5 / 1.75 스윕
5. payload_edges ON/OFF × penalty ON/OFF 4가지 조합 비교
6. correlated 시나리오 (g4)에서 특히 관찰 — penalty가 clustered 분포에서도 유효한지
```

---

---

## 종합: 두 논문의 pg_acorn 시사점

### 이론적 기여 (설명력)

- ACORN을 VJP로 분류한 survey 프레임워크가 현재 pg_acorn 아키텍처의 강점과 한계를 체계적으로 설명한다.
- Disconnected predicate subgraph 문제가 프로젝트에서 경험한 recall ceiling의 이론적 근거임을 확인한다.
- payload_gate (SJP)와 ACORN VJP의 조합이 서로 다른 selectivity 구간에 최적임을 프레임워크가 지지한다.

### 구현 가능성 (높은 순)

| 아이디어 | 구현 난이도 | 예상 효과 | 우선순위 |
|---------|------------|---------|---------|
| M-ACORN build penalty | 낮음 (2줄 수정 + GUC) | QPS +15~40% (SIFT1M 기준) | 1순위 |
| Wasserstein distance 벤치마크 metric | 낮음 (Python scipy/POT) | 논문 품질 향상 | 2순위 |
| Range filter (SeRF/iRangeGraph 참조) | 높음 (build 구조 변경) | 새 use-case 지원 | 장기 |
| StitchedVamana 식 per-partition subgraph | 높음 (major rebuild) | connectivity 완전 보장 | 장기 |

### 즉시 적용 권장 사항

**단기:**
1. `acorn_build.c`에 M-ACORN penalty 추가 — scan 경로 무변경, 리스크 최소
2. benchmark script에 Wasserstein-2 거리 계산 추가 — query hardness 정량화
3. payload_edges × penalty 조합 실험으로 두 메커니즘 시너지 확인

**중기:**
4. M 파라미터와 recall ceiling의 관계를 논문(VJP Dense Graph 요건)으로 문서화
5. 1M 스케일 M 스윕 실험으로 최소 안전 M 값 도출

---

*작성일: 2025-06-16*
*검토 논문: arxiv:2505.06501 (FANNS Survey), M-ACORN (연세대 DeLab, 2025)*
